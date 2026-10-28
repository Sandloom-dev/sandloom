# Platform owner console: operator guide

Sandloom Admin is the optional **platform owner and operations console**. It serves
trusted SaaS administrators across the deployment. An organization owner/admin
role alone cannot enter it. The UI, labels, help, and documentation are English;
customer-entered data retains its original language.

## Access and sign-in

Use an existing account with the global `admin` role, contributed by identity.
Enter email, password, and the current OTP if 2FA is enabled. The console does not
create a default administrator or embed a production password. Local demo
credentials belong only to the isolated preview database.

Tokens remain in browser memory. Reloading requires another sign-in. Logout,
revocation, role changes, and account deactivation clear affected sessions.
Buttons are a convenience; every operator API authorizes on the server with a
live session check. Platform administration does not add organization membership
or impersonate a customer.

## Finding your way

The **Overview** shows live service health per API process, what needs
attention (failed webhook events, operation intents awaiting follow-up) and the
latest audited operator activity. Press **⌘K** (Ctrl+K) anywhere to jump to a
page, find a user by name or email, or open any record by pasting its UUID.
Lists keep their search, filters, sort and page in the URL, so a view can be
shared or bookmarked; **Save view** stores a named filter set in this browser
only. Choose light, dark or system theme from the account menu.

## Customers and access

| Section | What it provides |
| --- | --- |
| Users | Search by name/email, filter by status, profile/status updates, assignment and revocation of existing platform roles, emergency account safety (below). Each user page shows the account's audit trail. |
| All organizations | Global active/inactive directory, detail cards, name/slug search, owner and active-status filters. |
| Organization members | Global directory filtered by organization or user ID; read only. |
| Billing customers | Personal or organization payment subjects; links to their subscriptions, balances, ledger, credit purchases, and intents. |
| Platform roles / Permissions | Existing RBAC management and permission catalog. System-role and last-administrator protections remain on the server. |

### Account safety: revoke sessions, block, unblock

- **Revoke sessions** ends every session of the user: refresh tokens are
  revoked and existing access tokens are rejected. The user can sign in again.
- **Block user** is the emergency stop: every request with an existing token is
  rejected, sign-in is refused (also for tokens minted later) and all sessions
  end. It stays until **Unblock user**. Retype the user's email to confirm.
- **Unblock user** lifts the block. Sessions ended by the block stay ended.

Each command needs a reason (stored in the audit log; no secrets or personal
data) and is recorded as `auth.user.blocked`, `auth.user.unblocked` or
`identity.user.sessions_revoked`. You cannot block or revoke your own account.
Several users can be selected in the list for a bulk command; each user is
processed as its own audited command.

The organization card links to its members and billing customer without requiring
membership. Existing organization self-service APIs retain their original scope;
this console does not expose membership changes, ownership transfer, or deletion
on behalf of another tenant.

## Billing

| Section | Meaning and available actions |
| --- | --- |
| Subscriptions | All synchronized subscriptions, customer/price references, provider status, current period and cancellation flags, authorization origin. Read only. |
| Products | Active and archived catalog entries. Edit the display name or availability with a support reason. Deactivating an offer does not cancel existing subscriptions. |
| Prices | Immutable local price mappings, currency, interval and purchase credit amounts. Monetary amounts are in minor currency units; credits are separate units. |
| Credit balances | Current ledger-derived balance, including negative balances caused by payment reversals. |
| Credit ledger | Append-only operations, delta, reason, operation reference and original resulting balance. Filter by customer ID. |
| Credit purchases | Verified credit grants, original payment/session references, purchased amount and reversed credits. |
| Refunds and disputes | Latest recorded refund/dispute state of payments seen by Sandloom. This is not the provider's complete payments feed. |
| Webhook journal | Event IDs, processing status and receive times. Raw payloads are not stored; the UI cannot replay an absent payload. |
| Operation intents | Checkout/portal authorization and reconciliation history. Filter `Followup = true` for operator investigation. Bearer checkout URLs, request hashes, PSP keys and raw provider errors are withheld. |

The dashboard and customer card identify the configured provider and demo/test/live
mode. For Stripe, fixed dashboard links open the independently authenticated
provider console for customer subscriptions, invoices, payments/refunds, disputes,
and product catalog. Sandloom does not pass its API key or a customer portal bearer URL
to those links. In demo mode there is no provider dashboard link.

### Adjust credits

1. Open **Billing customers** and select the correct personal/organization subject.
2. Inspect **Credit balance** and **Credit ledger**.
3. Choose **Adjust credits**, direction `grant` or `deduct`, positive amount,
   and a meaningful reason/support reference (8–300 characters).
4. Keep the generated operation reference and confirm the command.
5. Verify the ledger. This changes credits only; it never charges/refunds money.

The ledger dedupe key and adjustment commit in the same transaction as the audit.
Concurrent duplicate commands apply once. A retry returns the original balance,
not a later balance. Reusing the reference with a different amount, direction or
reason returns `409`. Deductions cannot overdraw; PSP reversals follow their
existing debt policy. Audit failure rolls the adjustment back.

No mutation is automatically retried. Every command shows its operation
reference. An unknown outcome retains the complete original request after
closing a dialog or navigating elsewhere. A confirmed success or definite
refusal completes that attempt; a new operation gets a fresh reference. If a response is lost (network failure, 502/503/504) the
dialog freezes its fields and offers **Retry with same reference**. The
session-wide recovery banner can also retry the original operation after leaving
the page. Other writes wait for resolution; reads remain available. Server
idempotency is still required for the declared operation. After a reload the
in-memory attempt is lost: inspect the ledger before creating another adjustment. Do not put passwords,
payment details, or personal information in the reason.

### Money, invoices and subscription changes

Perform monetary refunds, dispute responses, invoice operations and subscription
changes in Stripe using an account authorized for those operations. Sandloom's current
PSP port does not implement these commands. Verified signed webhooks synchronize
subscription and reversal state. Changing local subscription rows or inserting a
negative ledger entry is not a monetary refund.

Local price creation/import and arbitrary monetary price edits are not exposed.
Create/configure the provider catalog and its verified Sandloom mapping using the
existing billing deployment workflow. Do not alter an existing price's monetary
identity or credit entitlement after purchases.

For an intent follow-up, correlate customer, PSP session, event IDs and timestamps
with the secured provider/worker logs. Resolve the underlying payment at the
provider as appropriate and monitor webhook/reconciliation results. The UI has no
unsafe “mark processed” or blind “replay” button.

## Audit and service health

**Audit log** is one explorer over every module source (switch the source at the
top right). It reads committed metadata from the database hosting that module:
when, actor (shown by name), action, resource and request ID. Filter by a time
range (last hour … all time), an action prefix (`billing.` matches every billing
action; `_` and `%` are literal), actor ID, resource type and resource ID; the
filters live in the URL. Pages are keyset-paged newest first (**Newer** /
**Older**) — there is no total count, which keeps the query cheap on large
journals. In a monolith with one database every source shows the same journal;
in a split deployment each reads its service database. Free-form audit payloads
never reach the browser. An unsupported database driver answers `501`.

**Service health** shows one card per API process: its infrastructure,
configured peer-contract health and required consumer availability, re-sampled
every 30 seconds; use **Refresh** for a current sample. These checks can probe
dependencies; they are not a historic monitor, Docker inventory, or restart
console. A healthy API does not prove that its separate worker/beat process is
running; retain deployment health checks and monitoring.

## Event delivery

Choose the module source (in a split deployment: the API process) at the top.

- **Outbox dead letters**: events the relay could not hand to the broker within
  its attempt budget (`OUTBOX_RELAY_MAX_ATTEMPTS`). Select rows and **Requeue**
  with a reason: the same operation as `sandloom outbox requeue --id`. The envelope
  id is unchanged, so consumers that already applied an event deduplicate it.
  Recorded as `core.outbox.requeued` in the same transaction as the requeue.
- **Streams DLQ**: entries a consumer group of this process kept failing, per
  channel. **Replay** is targeted at the group that dead-lettered the entries
  (`sandloom streams dlq-replay --group`); every other group skips the replay.
  Select entries of one group; entries whose payload was lost cannot be
  replayed. Recorded as `core.streams.dlq_replayed` before the replay runs.
  Requires the durable Streams bus; otherwise the view answers "not available".
- **Consumers**: every event consumer of the process with its group, state,
  lag, pending deliveries, last connection and last error (redacted).
- **Projections**: query projections, their owner, storage and source
  channels; rebuild them with `sandloom projection rebuild` (never from the UI).

The overview lists dead letters of every source under **Needs attention**.

## Contracts, modules and settings

**Contracts & modules** lists the sync contracts the process provides, the
contracts each module consumes and how they resolve (local / remote /
missing), every remote binding with its reachability and compatibility
(`ok`, `unreachable`, `incompatible`, `unverified`), and the module load
result. **Runtime settings** shows an allowlist of non-secret settings and the
health of each infrastructure instance; secrets, URLs and DSNs are never sent.
Sandloom has no feature-flag capability, so the page says so instead of inventing
toggles. Change configuration through deployment, never from the console.

## Incident-list paging and record activity

Outbox and Streams DLQ tables have Previous/Next controls. A page is not a backlog
total; a `+` on the outbox badge means more rows exist. Requeue/replay affects only
the explicit selection and returns the list to its first page.

Activity is bound to the record's declared audit type and identifier. A resource
without a configured identity says so rather than guessing from an integer ID.
Use its filtered audit-explorer links for the full history beyond the first 50
entries. Bulk commands show individual refusals and retain only failed records
for correction; confirmed successes are not sent again during recovery.
