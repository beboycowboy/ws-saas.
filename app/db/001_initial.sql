CREATE EXTENSION IF NOT EXISTS btree_gist;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ws_app') THEN
        CREATE ROLE ws_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO ws_app;

CREATE TABLE tenants (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text NOT NULL UNIQUE,
 name text NOT NULL, timezone text NOT NULL, config_json jsonb NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE channel_accounts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 provider text NOT NULL, webhook_token_hash text NOT NULL UNIQUE, credentials_enc text,
 status text NOT NULL DEFAULT 'connected', last_seen_at timestamptz,
 UNIQUE (tenant_id,id), UNIQUE(tenant_id,provider)
);
CREATE TABLE owners (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants UNIQUE,
 telegram_chat_id text UNIQUE, email text NOT NULL, agenda_token_hash text NOT NULL UNIQUE,
 link_code_hash text UNIQUE, link_expires_at timestamptz,
 UNIQUE(tenant_id,id)
);
CREATE TABLE customers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 contact_id text NOT NULL, phone_e164 text, name text, consent_at timestamptz,
 opted_out boolean NOT NULL DEFAULT false, UNIQUE(tenant_id,contact_id), UNIQUE(tenant_id,id)
);
CREATE TABLE conversations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 customer_id uuid NOT NULL, mode text NOT NULL DEFAULT 'bot' CHECK(mode IN ('bot','human')),
 human_until timestamptz, state_json jsonb NOT NULL DEFAULT '{}', last_inbound_at timestamptz,
 last_handoff_notice_at timestamptz, version integer NOT NULL DEFAULT 0,
 FOREIGN KEY(tenant_id,customer_id) REFERENCES customers(tenant_id,id),
 UNIQUE(tenant_id,customer_id), UNIQUE(tenant_id,id)
);
CREATE TABLE messages (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 conversation_id uuid NOT NULL, channel_account_id uuid NOT NULL,
 provider_message_id text NOT NULL, direction text NOT NULL CHECK(direction IN ('in','out','owner')),
 text text NOT NULL, kind text NOT NULL DEFAULT 'text', provider_timestamp timestamptz NOT NULL,
 received_seq bigint GENERATED ALWAYS AS IDENTITY, processed_at timestamptz,
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id),
 FOREIGN KEY(tenant_id,channel_account_id) REFERENCES channel_accounts(tenant_id,id),
 UNIQUE(channel_account_id,provider_message_id), UNIQUE(tenant_id,id)
);
CREATE TABLE resources (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants UNIQUE,
 name text NOT NULL, UNIQUE(tenant_id,id)
);
CREATE TABLE services (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 code text NOT NULL, name text NOT NULL, minutes integer NOT NULL CHECK(minutes>0),
 buffer_minutes integer NOT NULL CHECK(buffer_minutes>=0), price_cents integer NOT NULL CHECK(price_cents>=0),
 active boolean NOT NULL DEFAULT true, UNIQUE(tenant_id,code), UNIQUE(tenant_id,id)
);
CREATE TABLE appointments (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 resource_id uuid NOT NULL, customer_id uuid NOT NULL, service_id uuid NOT NULL,
 starts_at timestamptz NOT NULL, service_ends_at timestamptz NOT NULL, ends_at timestamptz NOT NULL,
 status text NOT NULL DEFAULT 'confirmed' CHECK(status IN ('confirmed','cancelled','completed','no_show')),
 notes text, operation_key text NOT NULL,
 CHECK(starts_at<service_ends_at AND service_ends_at<=ends_at),
 FOREIGN KEY(tenant_id,resource_id) REFERENCES resources(tenant_id,id),
 FOREIGN KEY(tenant_id,customer_id) REFERENCES customers(tenant_id,id),
 FOREIGN KEY(tenant_id,service_id) REFERENCES services(tenant_id,id),
 UNIQUE(tenant_id,operation_key), UNIQUE(tenant_id,id),
 EXCLUDE USING gist (resource_id WITH =, tstzrange(starts_at,ends_at,'[)') WITH &&) WHERE(status='confirmed')
);
CREATE TABLE menu_items (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 code text NOT NULL, name text NOT NULL, price_cents integer NOT NULL CHECK(price_cents>=0),
 available boolean NOT NULL DEFAULT true, UNIQUE(tenant_id,code), UNIQUE(tenant_id,id)
);
CREATE TABLE orders (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 customer_id uuid NOT NULL, status text NOT NULL DEFAULT 'draft'
 CHECK(status IN ('draft','placed','accepted','rejected','cancelled','completed')),
 fulfillment text CHECK(fulfillment IN ('pickup','delivery')), address_note text,
 total_cents integer NOT NULL DEFAULT 0 CHECK(total_cents>=0), delivery_fee_cents integer NOT NULL DEFAULT 0,
 owner_eta_minutes integer CHECK(owner_eta_minutes IN (15,30,45)), version integer NOT NULL DEFAULT 0,
 FOREIGN KEY(tenant_id,customer_id) REFERENCES customers(tenant_id,id), UNIQUE(tenant_id,id)
);
CREATE UNIQUE INDEX one_cart_per_customer ON orders(tenant_id,customer_id) WHERE status='draft';
CREATE TABLE order_items (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 order_id uuid NOT NULL, menu_item_id uuid NOT NULL, qty integer NOT NULL CHECK(qty BETWEEN 1 AND 99),
 unit_price_cents integer NOT NULL CHECK(unit_price_cents>=0), note text,
 FOREIGN KEY(tenant_id,order_id) REFERENCES orders(tenant_id,id) ON DELETE CASCADE,
 FOREIGN KEY(tenant_id,menu_item_id) REFERENCES menu_items(tenant_id,id), UNIQUE(order_id,menu_item_id)
);
CREATE TABLE jobs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 conversation_id uuid, type text NOT NULL, payload_json jsonb NOT NULL DEFAULT '{}',
 run_at timestamptz NOT NULL, first_queued_at timestamptz NOT NULL,
 attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','done','dead')),
 locked_at timestamptz, lock_token uuid, last_error text,
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id)
);
CREATE UNIQUE INDEX one_pending_turn ON jobs(conversation_id) WHERE type='turn' AND status='pending';
CREATE INDEX jobs_due ON jobs(tenant_id,run_at) WHERE status IN ('pending','running');
CREATE INDEX messages_unprocessed ON messages(tenant_id,conversation_id,received_seq) WHERE processed_at IS NULL;
CREATE TABLE outbox (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 type text NOT NULL, event_key text NOT NULL, payload_json jsonb NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','dead')),
 attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz NOT NULL, last_error text,
 UNIQUE(tenant_id,event_key), UNIQUE(tenant_id,id)
);
CREATE INDEX outbox_due ON outbox(tenant_id,next_attempt_at) WHERE status='pending';
CREATE TABLE outbound (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 conversation_id uuid NOT NULL, channel_account_id uuid NOT NULL, customer_id uuid NOT NULL,
 event_key text NOT NULL, text text NOT NULL, run_at timestamptz NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','suppressed','dead')),
 attempts integer NOT NULL DEFAULT 0, allow_optout boolean NOT NULL DEFAULT false,
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id),
 FOREIGN KEY(tenant_id,channel_account_id) REFERENCES channel_accounts(tenant_id,id),
 FOREIGN KEY(tenant_id,customer_id) REFERENCES customers(tenant_id,id), UNIQUE(tenant_id,event_key)
);
CREATE INDEX outbound_due ON outbound(tenant_id,run_at) WHERE status='pending';
CREATE TABLE fake_channel_log (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 channel_account_id uuid NOT NULL, contact_id text NOT NULL, text text NOT NULL, delivery_key text NOT NULL,
 UNIQUE(tenant_id,delivery_key)
);
CREATE TABLE fake_notifications (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 outbox_id uuid NOT NULL, type text NOT NULL, payload_json jsonb NOT NULL,
 FOREIGN KEY(tenant_id,outbox_id) REFERENCES outbox(tenant_id,id), UNIQUE(outbox_id)
);
CREATE TABLE usage_ledger (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 conversation_id uuid, channel_account_id uuid, customer_id uuid,
 kind text NOT NULL CHECK(kind IN ('llm','msg_in','msg_out')),
 tokens_in integer NOT NULL DEFAULT 0, tokens_out integer NOT NULL DEFAULT 0,
 est_cost_micro_usd bigint NOT NULL DEFAULT 0,
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id)
);
CREATE INDEX usage_tenant_kind ON usage_ledger(tenant_id,kind);
CREATE TABLE audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 actor text NOT NULL, action text NOT NULL, entity text NOT NULL, entity_id uuid
);
CREATE TABLE tool_results (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 conversation_id uuid NOT NULL, operation_key text NOT NULL, result_json jsonb NOT NULL,
 FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id),
 UNIQUE(tenant_id,conversation_id,operation_key)
);
CREATE TABLE scheduled_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
 name text NOT NULL, period text NOT NULL, UNIQUE(tenant_id,name,period)
);
CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['channel_accounts','owners','customers','conversations','messages','resources',
 'services','appointments','menu_items','orders','order_items','jobs','outbox','outbound','fake_channel_log',
 'fake_notifications','usage_ledger','audit_events','tool_results','scheduled_runs'] LOOP
   EXECUTE format('ALTER TABLE %I ADD COLUMN created_at timestamptz NOT NULL DEFAULT now(), ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now()',t);
   EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = nullif(current_setting(''app.tenant_id'',true),'''')::uuid) WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'',true),'''')::uuid)',t);
   EXECUTE format('CREATE TRIGGER touch BEFORE UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION touch_updated_at()',t);
   EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO ws_app',t);
 END LOOP;
END $$;
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenants USING(id=nullif(current_setting('app.tenant_id',true),'')::uuid)
 WITH CHECK(id=nullif(current_setting('app.tenant_id',true),'')::uuid);
GRANT SELECT ON tenants TO ws_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ws_app;

-- Narrow bootstrap functions: no credentials, customer records or free-form SQL.
CREATE FUNCTION resolve_channel(token_hash text) RETURNS TABLE(id uuid, tenant_id uuid, provider text)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT c.id,c.tenant_id,c.provider FROM public.channel_accounts c JOIN public.tenants t ON t.id=c.tenant_id
 WHERE c.webhook_token_hash=token_hash AND t.status='active' $$;
CREATE FUNCTION resolve_agenda(token_hash text) RETURNS TABLE(id uuid, tenant_id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT o.id,o.tenant_id FROM public.owners o JOIN public.tenants t ON t.id=o.tenant_id
 WHERE o.agenda_token_hash=token_hash AND t.status='active' $$;
CREATE FUNCTION resolve_link(token_hash text) RETURNS TABLE(id uuid, tenant_id uuid, link_expires_at timestamptz)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT o.id,o.tenant_id,o.link_expires_at FROM public.owners o WHERE o.link_code_hash=token_hash $$;
CREATE FUNCTION work_tenants() RETURNS TABLE(id uuid)
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT t.id FROM public.tenants t WHERE t.status='active' $$;
REVOKE ALL ON FUNCTION resolve_channel(text),resolve_agenda(text),resolve_link(text),work_tenants() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_channel(text),resolve_agenda(text),resolve_link(text),work_tenants() TO ws_app;
