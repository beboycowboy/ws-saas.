ALTER TABLE outbound ADD COLUMN allow_human boolean NOT NULL DEFAULT false;
ALTER TABLE usage_ledger ADD COLUMN reservation_key text;
CREATE UNIQUE INDEX usage_reservation ON usage_ledger(tenant_id,reservation_key) WHERE reservation_key IS NOT NULL;
