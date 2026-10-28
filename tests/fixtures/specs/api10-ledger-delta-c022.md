# C022 remediation (2026-10-28)

- C-ROUTE-03: retain the analyzer and its negative controls. The three built-in
  mixed-authority routes now declare admin risk, as do the four owning tenancy
  mutation methods. Identity operator session revocation declares admin risk.
  The common revocation helper has one local, reasoned waiver: its callers own
  the authority and step-up policy. Replace the test that expected the three
  warnings with a check that the tree has no unreviewed sensitive effects.
- G-SENS-04: strengthen coverage for explicit `DbSession` dependencies. Their
  teardown committed after the runtime reset request admission, so automatic
  audit was skipped. Keep the request context until function dependencies exit;
  audit remains transactional and happens before the response. Test real built-in
  routes, combined route/effect tags (one automatic record), and operator risk
  propagation on local/RPC execution and durable replay.
- G-SENS-05: the same context lifetime correction restores the before-COMMIT
  money-dedupe check for explicit sessions; a route without a key returns 428
  and rolls its write back. The new injected-session regression covers it.

No new SDK name, risk category, permission grant, storage or dedupe mechanism.
Credential-changing effects retain security step-up; organizational changes and
operator revocation do not acquire a blanket five-minute login requirement.
The checkout-webhook budget changes from 14 to 15 PostgreSQL round trips solely
for its previously omitted mandatory audit INSERT; the test pins exactly one
such round trip. Other request budgets are unchanged.
