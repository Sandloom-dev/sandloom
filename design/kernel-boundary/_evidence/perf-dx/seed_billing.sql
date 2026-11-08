-- Billing rows for the platform seed (benchsuite.platform: USER owns ORG):
-- an active subscription and 100 credits for the user's personal account and
-- for the organization, so the live atoms reach an allow.
INSERT INTO billing_customers (id, user_id, organization_id) VALUES
  ('00000000-0000-4000-8000-00000000c001', '00000000-0000-4000-8000-00000000a001', NULL),
  ('00000000-0000-4000-8000-00000000c002', NULL, '00000000-0000-4000-8000-00000000b001')
ON CONFLICT DO NOTHING;
INSERT INTO billing_subscriptions (customer_id, status, current_period_end, cancel_at_period_end, origin)
SELECT c, 'active', now() + interval '30 days', false, 'legacy'
FROM (VALUES ('00000000-0000-4000-8000-00000000c001'::uuid), ('00000000-0000-4000-8000-00000000c002'::uuid)) v(c)
WHERE NOT EXISTS (SELECT 1 FROM billing_subscriptions s WHERE s.customer_id = v.c);
INSERT INTO billing_credit_balances (customer_id, amount)
SELECT c, 100
FROM (VALUES ('00000000-0000-4000-8000-00000000c001'::uuid), ('00000000-0000-4000-8000-00000000c002'::uuid)) v(c)
WHERE NOT EXISTS (SELECT 1 FROM billing_credit_balances b WHERE b.customer_id = v.c);
-- A catalog price: a subscription without a known price grants nothing.
INSERT INTO billing_products (id, name, type, is_active) VALUES (9001, 'Stand plan', 'subscription', true)
ON CONFLICT DO NOTHING;
INSERT INTO billing_prices (id, product_id, amount, currency, recurring_interval) VALUES (9001, 9001, 1000, 'usd', 'month')
ON CONFLICT DO NOTHING;
UPDATE billing_subscriptions SET price_id = 9001, origin = 'legacy' WHERE price_id IS NULL;
