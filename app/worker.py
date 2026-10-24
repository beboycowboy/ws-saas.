import time
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.conversation import process_turn
from app.core.tools import notify
from app.logging import event
from app.maintenance import purge_tenant
from app.notify.content import enrich


def lock(c, key):
    return c.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0)) ok", (key,)).fetchone()["ok"]


def unlock(c, key):
    c.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))


def work_turn(rt, tenant_id):
    now = rt.clock.now()
    with rt.db.pool.connection() as connection:
        with rt.db.tx(tenant_id, connection) as c:
            # Expired leases are recovered only after acquiring the conversation's session lock below.
            candidates = c.execute(
                """SELECT * FROM jobs WHERE (status='pending' AND run_at<=%s)
                OR (status='running' AND locked_at<%s) ORDER BY run_at LIMIT 20 FOR UPDATE SKIP LOCKED""",
                (now, now - timedelta(seconds=120)),
            ).fetchall()
            job = None
            for candidate in candidates:
                key = "conversation:" + str(candidate["conversation_id"])
                if lock(c, key):
                    job = candidate
                    break
            if job is None:
                return False
            job["lock_token"] = uuid4()
            c.execute(
                "UPDATE jobs SET status='running',locked_at=%s,lock_token=%s,attempts=attempts+1 WHERE id=%s",
                (now, job["lock_token"], job["id"]),
            )
        try:
            process_turn(rt, connection, tenant_id, job)
        except Exception as error:
            event("job_failed", tenant_id=tenant_id, job_id=job["id"], error_type=type(error).__name__)
            with rt.db.tx(tenant_id, connection) as c:
                attempts = job["attempts"] + 1
                status = "dead" if attempts >= job["max_attempts"] else "pending"
                other = c.execute(
                    "SELECT id FROM jobs WHERE conversation_id=%s AND status='pending'",
                    (job["conversation_id"],),
                ).fetchone()
                if other and status == "pending":
                    c.execute("UPDATE jobs SET status='done' WHERE id=%s", (other["id"],))
                c.execute(
                    "UPDATE jobs SET status=%s,run_at=%s,locked_at=NULL,last_error=%s WHERE id=%s",
                    (status, now + timedelta(seconds=min(300, 2**attempts)), type(error).__name__, job["id"]),
                )
                if status == "dead":
                    notify(
                        c,
                        tenant_id,
                        "admin_alert",
                        "dead:" + str(job["id"]),
                        {"reason": "job_dead", "job_id": str(job["id"])},
                        now,
                    )
        finally:
            unlock(connection, key)
    return True


def deliver_outbox(rt, tenant_id):
    now = rt.clock.now()
    with rt.db.pool.connection() as connection:
        with rt.db.tx(tenant_id, connection) as c:
            rows = c.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=%s ORDER BY created_at LIMIT 20",
                (now,),
            ).fetchall()
        for row in rows:
            key = "outbox:" + str(row["id"])
            if not lock(connection, key):
                continue
            try:
                with rt.db.tx(tenant_id, connection) as c:
                    current = c.execute("SELECT status FROM outbox WHERE id=%s", (row["id"],)).fetchone()
                if current["status"] != "pending":
                    continue
                try:
                    rt.notifier.send(enrich(rt, row))
                    with rt.db.tx(tenant_id, connection) as c:
                        c.execute(
                            "UPDATE outbox SET status='sent',attempts=attempts+1 WHERE id=%s", (row["id"],)
                        )
                except Exception as error:
                    attempts = row["attempts"] + 1
                    with rt.db.tx(tenant_id, connection) as c:
                        c.execute(
                            "UPDATE outbox SET attempts=%s,status=%s,next_attempt_at=%s,last_error=%s WHERE id=%s",
                            (
                                attempts,
                                "dead" if attempts >= 5 else "pending",
                                now + timedelta(seconds=2**attempts),
                                type(error).__name__,
                                row["id"],
                            ),
                        )
                        if attempts >= 5 and row["type"] != "admin_alert":
                            notify(
                                c,
                                tenant_id,
                                "admin_alert",
                                "outbox-dead:" + str(row["id"]),
                                {"reason": "notification_dead", "outbox_id": str(row["id"])},
                                now,
                            )
            finally:
                unlock(connection, key)
    return len(rows)


def deliver_outbound(rt, tenant_id):
    now = rt.clock.now()
    with rt.db.pool.connection() as connection:
        key = "outbound:" + str(tenant_id)
        if not lock(connection, key):
            return 0
        try:
            with rt.db.tx(tenant_id, connection) as c:
                rows = c.execute(
                    "SELECT * FROM outbound WHERE status='pending' AND run_at<=%s ORDER BY created_at LIMIT 20",
                    (now,),
                ).fetchall()
            for row in rows:
                with rt.db.tx(tenant_id, connection) as c:
                    conv = c.execute(
                        "SELECT * FROM conversations WHERE id=%s", (row["conversation_id"],)
                    ).fetchone()
                    customer = c.execute(
                        "SELECT * FROM customers WHERE id=%s", (row["customer_id"],)
                    ).fetchone()
                    account = c.execute(
                        "SELECT * FROM channel_accounts WHERE id=%s", (row["channel_account_id"],)
                    ).fetchone()
                    stale = not conv["last_inbound_at"] or conv["last_inbound_at"] < now - timedelta(
                        minutes=10
                    )
                    opted = customer["opted_out"] and not row["allow_optout"]
                    human = (
                        conv["mode"] == "human"
                        and conv["human_until"]
                        and conv["human_until"] > now
                        and not row["allow_human"]
                        and not row["allow_optout"]
                    )
                    if stale or opted or human:
                        c.execute("UPDATE outbound SET status='suppressed' WHERE id=%s", (row["id"],))
                        continue
                    usage = c.execute(
                        """SELECT count(*) FILTER(WHERE customer_id=%s AND created_at>=%s) contact,
                        count(*) FILTER(WHERE created_at>=%s) hourly,count(*) daily
                        FROM usage_ledger WHERE kind='msg_out' AND channel_account_id=%s AND created_at>=%s""",
                        (
                            row["customer_id"],
                            now - timedelta(hours=1),
                            now - timedelta(hours=1),
                            account["id"],
                            now - timedelta(days=1),
                        ),
                    ).fetchone()
                    if (
                        usage["contact"] >= rt.settings.rate_limits_contact_hour
                        or usage["hourly"] >= rt.settings.rate_limits_account_hour
                        or usage["daily"] >= rt.settings.rate_limits_account_day
                    ):
                        c.execute(
                            "UPDATE outbound SET run_at=%s WHERE id=%s",
                            (now + timedelta(minutes=1), row["id"]),
                        )
                        continue
                try:
                    account["delivery_key"] = str(row["id"])
                    rt.channel.send_typing(account, customer["contact_id"])
                    result = rt.channel.send_text(account, customer["contact_id"], row["text"])
                    with rt.db.tx(tenant_id, connection) as c:
                        c.execute(
                            "UPDATE outbound SET status='sent',delivered_at=%s WHERE id=%s",
                            (result.delivered_at, row["id"]),
                        )
                        c.execute(
                            """INSERT INTO messages(tenant_id,conversation_id,channel_account_id,provider_message_id,
                            direction,text,provider_timestamp,processed_at,created_at) VALUES(%s,%s,%s,%s,'out',%s,%s,%s,%s)
                            ON CONFLICT(channel_account_id,provider_message_id) DO NOTHING""",
                            (
                                tenant_id,
                                conv["id"],
                                account["id"],
                                "out:" + result.provider_message_id,
                                row["text"],
                                now,
                                now,
                                now,
                            ),
                        )
                        c.execute(
                            """INSERT INTO usage_ledger(tenant_id,conversation_id,channel_account_id,customer_id,kind,created_at)
                            VALUES(%s,%s,%s,%s,'msg_out',%s)""",
                            (tenant_id, conv["id"], account["id"], customer["id"], now),
                        )
                except Exception as error:
                    with rt.db.tx(tenant_id, connection) as c:
                        attempts = row["attempts"] + 1
                        c.execute(
                            "UPDATE outbound SET attempts=%s,status=%s,run_at=%s WHERE id=%s",
                            (
                                attempts,
                                "dead" if attempts >= 5 else "pending",
                                now + timedelta(seconds=2**attempts),
                                row["id"],
                            ),
                        )
                        if attempts >= 5:
                            notify(
                                c,
                                tenant_id,
                                "admin_alert",
                                "send-dead:" + str(row["id"]),
                                {"reason": "send_dead", "error_type": type(error).__name__},
                                now,
                            )
            return len(rows)
        finally:
            unlock(connection, key)


def scheduler(rt, tenant_id):
    now = rt.clock.now()
    with rt.db.tx(tenant_id) as c:
        tenant = c.execute("SELECT * FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
        local = now.astimezone(ZoneInfo(tenant["timezone"]))
        period = local.date().isoformat()
        if local.hour >= 8:
            inserted = c.execute(
                "INSERT INTO scheduled_runs(tenant_id,name,period) VALUES(%s,'daily',%s) ON CONFLICT DO NOTHING RETURNING id",
                (tenant_id, period),
            ).fetchone()
            if inserted:
                appointments = c.execute(
                    "SELECT count(*) n FROM appointments WHERE status='confirmed' AND (starts_at AT TIME ZONE %s)::date=%s",
                    (tenant["timezone"], local.date()),
                ).fetchone()["n"]
                orders = c.execute(
                    "SELECT count(*) n FROM orders WHERE status IN ('placed','accepted')"
                ).fetchone()["n"]
                usage = c.execute(
                    "SELECT coalesce(sum(tokens_in+tokens_out),0) tokens,coalesce(sum(est_cost_micro_usd),0) cost_micro_usd FROM usage_ledger WHERE created_at>=%s",
                    (now - timedelta(days=1),),
                ).fetchone()
                notify(
                    c,
                    tenant_id,
                    "daily_summary",
                    "daily:" + period,
                    {"date": period, "appointments": appointments, "open_orders": orders},
                    now,
                )
                notify(
                    c,
                    tenant_id,
                    "usage_report",
                    "usage:" + period,
                    {"date": period, **{k: int(v) for k, v in usage.items()}},
                    now,
                )
        heartbeat = str(int(now.timestamp()) // 120)
        inserted = c.execute(
            "INSERT INTO scheduled_runs(tenant_id,name,period) VALUES(%s,'heartbeat',%s) ON CONFLICT DO NOTHING RETURNING id",
            (tenant_id, heartbeat),
        ).fetchone()
        if inserted:
            accounts = c.execute("SELECT * FROM channel_accounts").fetchall()
            for account in accounts:
                if rt.channel.session_status(account) == "Disconnected":
                    notify(
                        c,
                        tenant_id,
                        "admin_alert",
                        "session:" + str(account["id"]) + ":" + heartbeat,
                        {"reason": "channel_disconnected"},
                        now,
                    )
        count = c.execute("SELECT count(*) n FROM jobs WHERE status='pending'").fetchone()["n"]
        if count > 50:
            notify(
                c,
                tenant_id,
                "admin_alert",
                "queue:" + heartbeat,
                {"reason": "queue_depth", "count": count},
                now,
            )
        failure_rate = c.execute(
            "SELECT count(*) n,count(*) FILTER(WHERE failed) failed FROM usage_ledger WHERE kind='llm' AND created_at>=%s",
            (now - timedelta(minutes=10),),
        ).fetchone()
        if failure_rate["n"] and failure_rate["failed"] / failure_rate["n"] > 0.1:
            notify(
                c,
                tenant_id,
                "admin_alert",
                "llm-rate:" + heartbeat,
                {
                    "reason": "llm_failure_rate",
                    "failures": failure_rate["failed"],
                    "calls": failure_rate["n"],
                },
                now,
            )
        maintenance = c.execute(
            "INSERT INTO scheduled_runs(tenant_id,name,period) VALUES(%s,'purge',%s) ON CONFLICT DO NOTHING RETURNING id",
            (tenant_id, period),
        ).fetchone()
        if maintenance:
            purge_tenant(c, now)
            c.execute("DELETE FROM scheduled_runs WHERE created_at<%s", (now - timedelta(days=14),))


def tick(rt, scheduled=True):
    count = 0
    due = scheduled and rt.clock.now() >= getattr(rt, "next_scheduler_at", rt.clock.now())
    if due:
        rt.next_scheduler_at = rt.clock.now() + timedelta(seconds=30)
    tenants = rt.db.tenant_ids()
    for tenant_id in tenants:
        if due:
            scheduler(rt, tenant_id)
        count += int(work_turn(rt, tenant_id))
        count += deliver_outbound(rt, tenant_id)
        count += deliver_outbox(rt, tenant_id)
    if due and tenants and rt.settings.app_env != "test":
        scheduled_backup(rt, sorted(tenants)[0])
    return count


def scheduled_backup(rt, tenant_id):
    from app.backup.service import create_backup

    now = rt.clock.now()
    period = now.date().isoformat()
    with rt.db.pool.connection() as connection:
        key = "daily-backup"
        if not lock(connection, key):
            return
        try:
            with rt.db.tx(tenant_id, connection) as c:
                if c.execute(
                    "SELECT 1 FROM scheduled_runs WHERE name='backup' AND period=%s", (period,)
                ).fetchone():
                    return
                # Failed attempts are limited to one every ten minutes across workers.
                attempt = str(int(now.timestamp()) // 600)
                if not c.execute(
                    "INSERT INTO scheduled_runs(tenant_id,name,period) VALUES(%s,'backup-attempt',%s) ON CONFLICT DO NOTHING RETURNING id",
                    (tenant_id, attempt),
                ).fetchone():
                    return
            try:
                create_backup(rt)
                with rt.db.tx(tenant_id, connection) as c:
                    c.execute(
                        "INSERT INTO scheduled_runs(tenant_id,name,period) VALUES(%s,'backup',%s) ON CONFLICT DO NOTHING",
                        (tenant_id, period),
                    )
            except Exception as error:
                with rt.db.tx(tenant_id, connection) as c:
                    notify(
                        c,
                        tenant_id,
                        "admin_alert",
                        "backup:" + attempt,
                        {"reason": "backup_failed", "error_type": type(error).__name__},
                        now,
                    )
        finally:
            unlock(connection, key)


def run(rt):
    event("worker_started")
    while True:
        tick(rt)
        time.sleep(0.25)
