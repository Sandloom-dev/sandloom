---
title: Service RPC
type: concept
---

# Service RPC

A contract call is the same code whether its provider runs in the process or
in another service ([local equals remote](../modules/between/contracts.md#local-equals-remote)).
In a split deployment you configure where each contract lives, the keys that
authenticate the call, the allowlist and the transport.

## Resolution order

1. **Local** service-registry entry, when the provider module is co-deployed.
2. **Remote** generated proxy, when `SERVICE_BINDINGS` has the requested service.
3. `ServiceNotFound`, preserving the optional-degrade path.

```bash
SANDLOOM_MODULES='["sandloom_billing"]'
SERVICE_BINDINGS='{"tenancy.membership.v1":{"base_url":"http://tenancy:2222","service_id":"tenancy"}}'
RPC_SERVICE_ID='billing'
# + this service's RPC keys and the shared key maps / allowlist (below)
```

## Provider endpoint

Every process installs an internal endpoint:

```text
POST /_sandloom/rpc/{service_name}/{method_name}
GET  /_sandloom/rpc/{service_name}        # readiness status / describe document
```

It accepts only pair-MAC authenticated requests (below); a request carrying
no RPC credential gets a hidden `404`. `INTERNAL_API_TOKEN` guards only the
ops endpoints (`/metrics`, `/diagnostics/*`, `/contracts/*`, `/health`
details) and never authenticates `/_sandloom/rpc`; `INTERNAL_ENDPOINTS_OPEN_IN_DEV`
never opens it.

An unreachable provider degrades only the calls that need it. `/ready` fails on it
only when its binding sets `"required": true`; the default is `false`.

## Request authentication and idempotency

There is one internal-RPC authentication
([ADR-0004](../adr/0004-rpc-pair-mac-request-authentication.md)):

- **Pair MAC on every call.** Each request is HMACed under a per-pair,
  directional key HKDF-derived from the caller's and the provider's X25519
  keys (`RPC_DH_PRIVATE_KEY` + `RPC_DH_KEY_ID` on each service, public halves
  in `RPC_SERVICE_DH_KEYS` everywhere) — audience-bound, with a per-attempt
  nonce (replay guard `RPC_REPLAY_GUARD`, window
  `RPC_AUTH_MAX_SKEW_SECONDS`).
- **Ed25519 signature on commands.** A calling service also signs command
  calls with its own key (`RPC_PRIVATE_KEY` + `RPC_KEY_ID`, public halves in
  `RPC_SERVICE_KEYS`); `RPC_COMMANDS_REQUIRE_SIGNATURE` (`auto` = on in prod,
  off in dev/test; `all` signs reads too).
- **Deny-by-default allowlist.** The provider allows only what
  `RPC_ALLOWED_CALLS` lists for the caller (`403` otherwise).

Every `SERVICE_BINDINGS` entry pins its provider with `"service_id"`; a
process with bindings must hold its own `RPC_DH_PRIVATE_KEY` (startup refuses
otherwise); a provider without one refuses every RPC call. A monolith (no
bindings) needs no RPC keys. Generate keys with `sandloom keys generate --purpose
rpc --service <id>` (Ed25519 + X25519) or `--purpose rpc-dh` (X25519 only),
and the allowlist with `sandloom keys allowlist`.

- The caller's bound tenant travels in the MAC-covered context
  (`X-SANDLOOM-RPC-Context`); the provider re-binds the tenant **only** from it. A
  bare `X-Tenant-Id` header is rejected (`400`). The context carries the
  tenant only — a contract acting for a user takes the user id as an explicit
  argument.
- Reads keep no idempotency record. A command is deduped by the contract
  runtime's single record, shared by the local and the remote call, so the
  proxy's single transport retry never runs a command twice.

## Errors

What a caller receives for each RPC failure is in
[Contracts](../modules/between/contracts.md#local-equals-remote). When
diagnosing, note that every refusal at the provider (a bad MAC, a replay, a
caller missing from `RPC_ALLOWED_CALLS`) and every reply without a keyed
binding reaches the caller as `503 RemoteServiceUnavailable`, never as the
provider's 401 or 403. Its `code` (`rpc_auth_*`, `rpc_identity_*`,
`rpc_context_*`, `rpc_caller_forbidden`, `rpc_replay_unavailable`) tells a key
or allowlist mistake from a provider that is down.

## Transport & performance

The internal RPC is **HTTP with a MessagePack body** over a **single pooled
keep-alive client** (`sandloom.core.rpc`):

- **MessagePack, not JSON** — binary, compact, fast, and still *schemaless*: the
  contract is a plain Python method, no IDL and no codegen.
- **Pooled connections** — a process-wide client (`RPC_TRANSPORT`: aiohttp by
  default, httpx as the fallback) reuses connections, removing the per-call
  TCP+TLS handshake; disposed on shutdown.

gRPC was evaluated and declined: the sync axis carries small, infrequent point
lookups (bulk flows go through the event bus), so protobuf's wire win is dwarfed
by network + DB time, while it would cost an IDL/codegen step and break the
"contracts are Python, no codegen" property.

## Response binding

Every response is bound to the request that asked for it (a per-attempt
nonce + a MAC over the nonce, the request's authentication, status and body),
keyed with a per-pair secret from the X25519 key agreement of the two
services' dedicated X25519 keys, and accepted only from the provider the
binding pins (`"service_id"`). Unbound or unkeyed replies are always refused;
an unkeyed error answer (the provider could not authenticate the request)
surfaces as `503 RemoteServiceUnavailable` with the provider's error code.
Binding authenticates responses but is **not** a substitute for TLS/mTLS: run
internal RPC over TLS or a trusted network. See the
[security model](../advanced/security-model.md).
