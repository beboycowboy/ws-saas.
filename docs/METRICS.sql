-- Run as an administrative read-only session. Change the reporting interval if needed.
-- Handoff is a count of distinct conversations with an explicit handoff/take action.
-- Abandonment below is a proxy: draft orders older than one day, not an asserted intent classifier.
WITH conversations_in_period AS (
 SELECT tenant_id,id FROM conversations WHERE created_at>=now()-interval '7 days'
), message_counts AS (
 SELECT tenant_id,count(*) messages FROM messages WHERE created_at>=now()-interval '7 days' GROUP BY tenant_id
), bookings AS (
 SELECT tenant_id,count(*) appointments FROM appointments WHERE created_at>=now()-interval '7 days' GROUP BY tenant_id
), purchases AS (
 SELECT tenant_id,count(*) FILTER(WHERE status<>'draft') orders,
 count(*) FILTER(WHERE status='draft' AND created_at<now()-interval '1 day') abandoned_drafts
 FROM orders WHERE created_at>=now()-interval '7 days' GROUP BY tenant_id
), handoffs AS (
 SELECT tenant_id,count(DISTINCT entity_id) handoffs FROM audit_events
 WHERE entity='conversation' AND action IN ('handoff','take') AND created_at>=now()-interval '7 days' GROUP BY tenant_id
), costs AS (
 SELECT tenant_id,sum(est_cost_micro_usd) cost_micro_usd FROM usage_ledger
 WHERE created_at>=now()-interval '7 days' GROUP BY tenant_id
), volumes AS (
 SELECT tenant_id,count(*) conversations FROM conversations_in_period GROUP BY tenant_id
)
SELECT t.slug,coalesce(v.conversations,0) conversations,coalesce(b.appointments,0) appointments,
 coalesce(p.orders,0) orders,coalesce(h.handoffs,0) handoffs,
 round(100.0*coalesce(h.handoffs,0)/nullif(v.conversations,0),2) handoff_percent,
 coalesce(p.abandoned_drafts,0) abandoned_drafts,coalesce(m.messages,0) messages,
 round(coalesce(m.messages,0)::numeric/nullif(v.conversations,0),2) messages_per_conversation,
 coalesce(c.cost_micro_usd,0) cost_micro_usd,
 round(coalesce(c.cost_micro_usd,0)::numeric/nullif(v.conversations,0),2) cost_micro_usd_per_conversation
FROM tenants t LEFT JOIN volumes v ON v.tenant_id=t.id LEFT JOIN bookings b ON b.tenant_id=t.id
LEFT JOIN purchases p ON p.tenant_id=t.id LEFT JOIN handoffs h ON h.tenant_id=t.id
LEFT JOIN message_counts m ON m.tenant_id=t.id LEFT JOIN costs c ON c.tenant_id=t.id ORDER BY t.slug;
