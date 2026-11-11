-- Run after benchsuite.platform seed and seed_billing.sql, in a dedicated
-- benchmark database. Claims are read from real authority data by the mint.
INSERT INTO users (id, email, full_name, is_active, is_superuser, is_verified, created_at, updated_at)
SELECT id::uuid, email, 'Authz stand', true, false, true, now(), now()
FROM (VALUES
 ('00000000-0000-4000-8000-00000000a002', 'authz-member@example.com'),
 ('00000000-0000-4000-8000-00000000a003', 'authz-user@example.com'),
 ('00000000-0000-4000-8000-00000000a004', 'authz-pastdue@example.com'),
 ('00000000-0000-4000-8000-00000000a0ff', 'authz-outsider@example.com')
) v(id, email) ON CONFLICT (id) DO NOTHING;

INSERT INTO roles (id, name, code, is_system, created_at, updated_at)
VALUES ('00000000-0000-4000-8000-00000000d001', 'Stand admin', 'admin', false, now(), now())
ON CONFLICT (code) DO NOTHING;
INSERT INTO permissions (id, name, code, is_system, created_at)
VALUES ('00000000-0000-4000-8000-00000000d002', 'Stand read', 'perm.stand.read', false, now())
ON CONFLICT (code) DO NOTHING;
INSERT INTO user_roles (id, user_id, role_id, assigned_at)
SELECT '00000000-0000-4000-8000-00000000d003', '00000000-0000-4000-8000-00000000a001', id, now()
FROM roles WHERE code = 'admin' ON CONFLICT DO NOTHING;
INSERT INTO role_permissions (id, role_id, permission_id, assigned_at)
SELECT '00000000-0000-4000-8000-00000000d004', r.id, p.id, now()
FROM roles r, permissions p WHERE r.code = 'admin' AND p.code = 'perm.stand.read'
ON CONFLICT DO NOTHING;

INSERT INTO tenancy_organizations (id, name, slug, owner_id, is_active, created_at, updated_at)
VALUES ('00000000-0000-4000-8000-00000000b002', 'Inactive plan org', 'authz-inactive-plan',
 '00000000-0000-4000-8000-00000000a001', true, now(), now()) ON CONFLICT DO NOTHING;
INSERT INTO tenancy_members (id, organization_id, user_id, role, created_at)
VALUES
 ('00000000-0000-4000-8000-00000000d005', '00000000-0000-4000-8000-00000000b001',
  '00000000-0000-4000-8000-00000000a002', 'member', now()),
 ('00000000-0000-4000-8000-00000000d006', '00000000-0000-4000-8000-00000000b002',
  '00000000-0000-4000-8000-00000000a001', 'owner', now()) ON CONFLICT DO NOTHING;

INSERT INTO billing_customers (id, user_id, organization_id) VALUES
 ('00000000-0000-4000-8000-00000000c003', '00000000-0000-4000-8000-00000000a003', NULL),
 ('00000000-0000-4000-8000-00000000c004', '00000000-0000-4000-8000-00000000a004', NULL),
 ('00000000-0000-4000-8000-00000000c005', NULL, '00000000-0000-4000-8000-00000000b002')
ON CONFLICT DO NOTHING;
INSERT INTO billing_credit_balances (customer_id, amount)
VALUES ('00000000-0000-4000-8000-00000000c003', 100) ON CONFLICT DO NOTHING;
INSERT INTO billing_subscriptions (customer_id, status, price_id, current_period_end, cancel_at_period_end, origin)
SELECT customer_id::uuid, status, 9001, now() + interval '30 days', false, 'legacy'
FROM (VALUES
 ('00000000-0000-4000-8000-00000000c004', 'past_due'),
 ('00000000-0000-4000-8000-00000000c005', 'canceled')
) v(customer_id, status)
WHERE NOT EXISTS (SELECT 1 FROM billing_subscriptions s WHERE s.customer_id = v.customer_id::uuid);
