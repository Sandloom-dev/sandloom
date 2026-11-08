-- PoC kb-poc-authz: subjects beyond the perf-dx seed (benchsuite.platform seed +
-- seed_billing.sql): MEMBER (role 'member' in ORG) and ORG2, where USER is
-- 'admin' and the organization's subscription is canceled (no access).
INSERT INTO users (id, email, full_name, is_active, is_superuser, is_verified, created_at, updated_at)
VALUES ('00000000-0000-4000-8000-00000000a002', 'member@example.com', 'Member', true, false, true, now(), now())
ON CONFLICT (id) DO NOTHING;
INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
VALUES ('00000000-0000-4000-8000-0000000d0002', '00000000-0000-4000-8000-00000000b001',
        '00000000-0000-4000-8000-00000000a002', 'member', now())
ON CONFLICT (organization_id, user_id) DO NOTHING;
INSERT INTO tenancy_organizations (id, name, slug, owner_id, is_active, created_at, updated_at)
VALUES ('00000000-0000-4000-8000-00000000b002', 'Inactive Org', 'inactive-org',
        '00000000-0000-4000-8000-00000000a002', true, now(), now())
ON CONFLICT (id) DO NOTHING;
INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
VALUES ('00000000-0000-4000-8000-0000000d0003', '00000000-0000-4000-8000-00000000b002',
        '00000000-0000-4000-8000-00000000a001', 'admin', now())
ON CONFLICT (organization_id, user_id) DO NOTHING;
INSERT INTO billing_customers (id, user_id, organization_id) VALUES
  ('00000000-0000-4000-8000-00000000c003', NULL, '00000000-0000-4000-8000-00000000b002')
ON CONFLICT DO NOTHING;
INSERT INTO billing_subscriptions (customer_id, status, current_period_end, cancel_at_period_end, origin, price_id)
SELECT '00000000-0000-4000-8000-00000000c003', 'canceled', now() - interval '1 day', false, 'legacy', 9001
WHERE NOT EXISTS (SELECT 1 FROM billing_subscriptions s
                  WHERE s.customer_id = '00000000-0000-4000-8000-00000000c003');
