# ADR-0004: Internal RPC request authentication with per-pair HMAC keys

- **Status:** Accepted (draft — awaiting the maintainers' sign-off).
  Amended 2026-10-25: the pair MAC is the only
  internal-RPC authentication; `RPC_AUTH_MODE` and `RPC_RESPONSE_BINDING`
  are removed — setting either fails startup unless it is set to its inert
  value (see "Amendment (2026-10-25)"). The sections below that compare
  modes describe the migration that the amendment completed
- **Date:** 2026-10-21
- **Deciders:** Sandloom maintainers
- **Supersedes:** — (extends the service-identity model of #57 and the keyed
  response binding of `docs/SERVICE_RPC.md` → "Response binding")

## Context

In `RPC_AUTH_MODE=service_identity` every internal RPC call (and every
describe / readiness probe) is signed by the caller with its own Ed25519 key
(`sandloom.core.rpc_identity`); the provider verifies the signature with the
caller's public key from `RPC_SERVICE_KEYS` and authorizes the caller against
`RPC_ALLOWED_CALLS`. The native-acceleration research
(`docs/design/native-acceleration-research-2026-10-21.md`, R2, branch
`claude/confident-sammet-a122dc`) measured the cost:

| primitive (cryptography 50 / OpenSSL, arm64) | Ir / op (cachegrind) | wall µs |
|---|---|---|
| Ed25519 verify (provider, every call) | 695 k = **28 %** of the provider side of a call | 86 |
| Ed25519 sign (caller, every call) | 230 k | 29 |
| HMAC-SHA256, prepared key context | 14.5 k | 1.1 |

A Rust verifier (`ed25519-dalek`) would only buy 2.1× at the price of a native
crypto dependency. Every service already holds a **dedicated static X25519
key** for the keyed response binding (`RPC_DH_PRIVATE_KEY`, public halves in
`RPC_SERVICE_DH_KEYS`), and both ends of every pair can already derive a
shared per-pair secret. The 2026-10-21 RPC review also found:

1. **No audience.** The Ed25519 signature covers method, target, body digest
   and context, but not the *provider*. A request captured on the wire can be
   replayed to **another service that serves the same contract** (two
   replicas sets, a staging/prod pair sharing keys, a contract hosted by two
   services) within the 300 s context window.
2. **Identity failures surface as 401/403 to end users.** `rpc_identity_*` and
   `rpc_caller_forbidden` were not in the caller's "unavailable" code table,
   so a provider's 401/403 became a `ContractCallError` with the same status
   — a deployment misconfiguration answered the end user "unauthorized".
3. `RPC_RESPONSE_BINDING=strict` accepts `alg=token` bindings without a
   pinned `service_id` (token deployments only; see "Follow-ups").

## Decision

Add a request authenticator that is a **MAC under a per-pair, directional,
HKDF-derived key** from the services' existing X25519 keys, behind two new
`RPC_AUTH_MODE` values, and make it the target mode of split deployments:

| `RPC_AUTH_MODE` | caller sends | provider accepts |
|---|---|---|
| `shared_token` | token + context HMAC | token |
| `dual` | token + Ed25519 | token or Ed25519 |
| `service_identity` | Ed25519 (+ `aud` when the binding is pinned) | Ed25519 |
| **`service_mac_dual`** (migration) | **pair MAC** + Ed25519 | pair MAC if present (never falls back once present), else Ed25519 |
| **`service_mac`** (target) | **pair MAC**; **commands** also Ed25519-signed | **pair MAC**, plus the caller's Ed25519 signature on **commands** (`RPC_COMMANDS_REQUIRE_SIGNATURE`, on in prod) — Ed25519-only / token requests are refused |

The command signature is the mitigation of **key-compromise impersonation**
(see "Key-compromise impersonation" below): the pair key is symmetric, so
whoever holds a provider's X25519 private key can compute it for every
caller. Effects therefore also need a proof only the caller can produce;
reads — the hot path — stay MAC-only.

Implementation: `sandloom.core.rpc_pair_mac` (key schedule, canonical encoding,
MAC, header, replay guard, configuration checks); wiring in `sandloom.core.rpc`.
All primitives come from `cryptography` (X25519, HKDF) and the stdlib
(`hmac`, `hashlib`); there is no custom crypto code.

### Key schedule

For a caller `C` (X25519 key `c`, kid `ck`) and a provider `P` (key `p`, kid
`pk`), both sides compute

```text
Z      = X25519(c, P_pub) = X25519(p, C_pub)          # cryptography's exchange
         reject: exchange error (low-order / non-canonical point) or Z == 0^32
K_req  = HKDF-SHA256(IKM = Z,
                     salt = "sandloom-rpc-pair-mac/v1",
                     info = LP("sandloom-rpc-request-key", "1", "caller->provider",
                               C, ck, P, pk),
                     L = 32)
```

`LP(...)` is the canonical length-prefixed encoding below. The identities are
in **role order** (caller first, provider second) with an explicit direction
label, which is canonical: both sides know which role each id plays, and
`K_req(C→P) ≠ K_req(P→C)` (the reverse direction is a different key — a
provider can never turn a request it received into one it sends). Keys per
pair and direction:

| key | salt / info | used for |
|---|---|---|
| `K_req(C→P)` (new) | `sandloom-rpc-pair-mac/v1`, `LP("sandloom-rpc-request-key","1","caller->provider",C,ck,P,pk)` | caller → provider request MAC |
| `K_resp(P→C)` (existing v2 response binding) | `sandloom-rpc-response-binding/v2`, `"response"\0C\0ck\0P\0pk` | provider → caller response MAC |

Distinct salts *and* infos make the keys independent HKDF outputs of the same
`Z`: neither key ever MACs the other direction's messages, and no X25519 key
is used for anything but key agreement (the Ed25519 signing key stays
separate; startup refuses a DH key whose 32 bytes equal the signing seed).
No third "response" key is introduced: the provider→caller direction is
already served, domain-separated, by `K_resp`. Derived keys are cached as
prepared HMAC contexts keyed by (own private PEM, peer public PEM, info); the
info names both kids, so a rotated key is a new cache entry and the cache is
bounded (cleared when full) and cleared explicitly by `clear_key_cache()`.

### Request MAC

Headers (one call attempt):

```text
X-SANDLOOM-RPC-Nonce:   32 lowercase hex = 128 random bits (os.urandom), fresh per attempt
X-SANDLOOM-RPC-Context: base64url(JSON {tenant_id, idk, iat[, aud, svc, kid, dhk]})
X-SANDLOOM-RPC-Auth:    v=1;caller=<C>;ck=<ck>;aud=<P>;pk=<pk>;mac=<base64url(32-byte tag)>
```

```text
tag = HMAC-SHA256(K_req(C→P),
        LP("sandloom-rpc-request-mac", "1", C, ck, P, pk,
           HTTP method, target "<contract>/<method>" (GET: "<contract>"),
           nonce, raw context bytes, SHA-256(raw body)))
LP(x1..xn) = for each xi: uint32_be(len(xi)) || xi        (fixed arity n)
```

- **Unambiguous.** A fixed number of fields, each with a 4-byte length
  prefix: the encoding is injective (it has a decoder; the property tests
  check `decode(encode(x)) == x` and that no two distinct inputs collide).
- **Covers everything the provider acts on.** The provider authorizes the
  caller id from the header (the one the key was derived from), serves the
  target from the URL, binds tenant / idempotency key / issue time from the
  context bytes, and executes the body — all inside the tag. The context is
  covered as the exact bytes the provider parses (it is parsed only after the
  tag verified), so no JSON canonicalisation is needed; duplicate JSON keys
  are refused anyway. Future context fields (actor, deadline) are covered
  automatically.
- **Audience by construction.** `P` and `pk` are in the key *and* in the MAC
  input; the provider requires `aud == RPC_SERVICE_ID` and `pk` ∈ its own
  kids. A request for P never verifies at Q (Q derives `K_req(C→Q) ≠
  K_req(C→P)`).
- **The response covers the request MAC.** The keyed response binding's MAC
  input includes the request's `X-SANDLOOM-RPC-Auth` (with the signature
  headers), so a pair-keyed response verifies only for the exact request it
  answers — never for another request re-labelled with its nonce.
- **Order (S5 kept).** The provider reads the raw body under the body cap,
  verifies the tag over the raw bytes, and only then decodes the context and
  the MessagePack body. Comparison is `hmac.compare_digest` on the full
  32-byte tag; a tag that does not decode to exactly 32 bytes (truncated,
  padded, non-base64url) is rejected before any comparison.

### Replay

- **Freshness.** `|now − iat| ≤ RPC_AUTH_MAX_SKEW_SECONDS` (default 60 s,
  5…300) in the MAC modes; `iat` is integer seconds from the caller's clock.
  The rule is symmetric: a caller up to the skew ahead or behind is accepted;
  beyond it the call fails `rpc_auth_expired` (→ 503 at the caller; alert on
  `sandloom_rpc_auth_rejected_total{reason="expired"}` — a drifting node). The
  Ed25519 path keeps its 300 s window.
- **Nonce replay guard.** After the tag verified (only authenticated callers
  can insert entries), the provider records `(caller, nonce)` until the
  request could no longer pass the freshness check on its own clock
  (`iat + skew − now + 2` s, at most `2·skew + 2`). A repeat inside that
  window is `401 rpc_auth_replayed`. The nonce guard runs only AFTER the
  caller is authorized (`RPC_ALLOWED_CALLS`): a key holder without an
  allowlist entry cannot fill it.
  - Layer 1, always: a per-process bounded table
    (`RPC_REPLAY_CACHE_MAX_ENTRIES`, default 200 000 ≈ 2·60 s at 1 600
    calls/s per process, ≈ 40 MB worst case), of which one caller may hold
    at most `RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES` (default 100 000 =
    half the table). Sizing: a nonce lives ≈ `skew + 2` s (worst case
    `2·skew + 2`), so a caller needs at least its peak calls/s per provider
    process × `(2·skew + 2)` — the default carries ≈ 830 calls/s per caller
    per process at `skew = 60`; alert on
    `sandloom_rpc_auth_rejected_total{reason="caller_quota"}`: an
    allowed peer flooding it (e.g. `iat = now + skew` at full rate) is
    refused itself (`503`, reason `caller_quota`), the other callers are not.
    When the table is full of live entries the provider fails closed
    (`503 rpc_replay_unavailable`) instead of evicting a live nonce; it is
    never rebuilt on reconfiguration.
  - Layer 2, `RPC_REPLAY_GUARD=commands` (default) or `all`: an atomic
    `SET NX` with that TTL on the **security cache** (`SECURITY_CACHE_INSTANCE`,
    shared by every replica and worker) for command calls (`commands`) or
    every call (`all`); `local` disables it. The shared key is a SHA-256 of
    `LP(provider, caller, nonce)` and lives a constant `3·skew` s — not
    derived from one replica's clock: a request is accepted by replicas over
    at most `2·skew + d` of real time, `d` being the clock difference between
    two replicas, and the deployment rule (the same one callers meet) is
    `d ≤ skew` — a deployment requirement (`deploy/README.md`), watched by
    `sandloom_rpc_auth_clock_skew_total{caller}` (an accepted request off by more
    than half the window; one log line a minute per caller). A cache outage fails closed in production (`503
    rpc_replay_unavailable`, the caller retries) and open outside it (logged,
    counted; a startup warning says so). `RPC_REPLAY_GUARD=local` is refused
    in production.
  - **Multi-replica limit.** With `local` (or for reads under `commands`) a
    replay that lands on another worker process or replica within the window
    is not detected. That is accepted for reads (no effect; an on-path party
    can read the response anyway without TLS) and is why commands default to
    the shared layer — on top of the durable command dedupe
    (`contract_command_log`), which already turns a replayed command with the
    same idempotency key into a replay of its stored result.
- **Transport retry.** The proxy's single retry uses a new nonce and a new
  tag, so it is never mistaken for a replay.

### Downgrade

- A provider in `service_mac` accepts only `X-SANDLOOM-RPC-Auth`. A request with
  only an Ed25519 signature gets `401 rpc_auth_required` (a caller still on
  `service_identity`: an operator error worth naming); a request with only
  `X-Internal-Token`, or with no credential at all, keeps the hidden `404`
  of `service_identity`. Ed25519 signatures are never verified in this mode.
- In `service_mac_dual` a present `X-SANDLOOM-RPC-Auth` is the **only** credential
  checked: a bad MAC is `401` even when a valid Ed25519 signature rides along
  (no fallback an attacker could steer). A dual caller marks the context it
  MACs (`"mac": 1`, inside the signed context); a MAC-mode provider refuses
  such a context on the Ed25519 path (`401 rpc_auth_required`), so stripping
  the MAC cannot skip the nonce guard. Only a `service_identity` caller's
  request (no flag) uses the Ed25519 path — its context carries `aud` when
  the binding is pinned (see below). A `service_identity` provider
  (mid-rollout) ignores the flag.
- A request carrying any of `X-SANDLOOM-RPC-Auth`, `X-SANDLOOM-RPC-Nonce`,
  `X-SANDLOOM-RPC-Context`, the signature headers more than once is refused
  (`401`) and its response is never bound: the verifier and the response
  binder can never read two different nonces.
- The caller never falls back: in a MAC mode a call whose pair key cannot be
  derived (binding without `service_id`, provider without published X25519
  key, low-order key) fails `503 rpc_auth_unavailable`; startup refuses that
  configuration (below), so this only happens on a live misconfiguration.
- Unknown header versions (`v≠1`), unknown or duplicated header parameters,
  and unknown caller / provider kids are `401 rpc_auth_invalid`.
- `RPC_ALLOWED_CALLS` and deny-by-default are unchanged; the caller id the
  allowlist is consulted with is the verified `caller` of the MAC header.

### Key-compromise impersonation (KCI) and the command signature

Static-static ECDH is symmetric: `X25519(c, P) = X25519(p, C)`. Whoever holds
**a provider P's X25519 private key** — a leaked secret, a stolen config, a
retired key kept in `RPC_DH_EXTRA_PRIVATE_KEYS`; code execution is not needed
— derives `K_req(C→P)` for **every** caller C and can present requests to P
as the most privileged caller (`RPC_ALLOWED_CALLS` on P is then moot). With
Ed25519, the same leak gave only P's *outbound* rights. A provider's X25519
key is therefore an **"all inbound rights"** secret, on top of being its
callers'-response key.

Mitigation (decided):

- `RPC_COMMANDS_REQUIRE_SIGNATURE` (`auto` = on in prod; `on`; `all`;
  `off`, refused under `multi_tenant_strict`): a **command** call (any
  method that is not a declared read, and every not-yet-described method)
  additionally carries the caller's Ed25519 signature over the same context,
  target and body (`X-SANDLOOM-RPC-Service-Signature`, the `service_identity`
  proof). The signed context carries the attempt's nonce (`n`; the caller
  re-signs per transport attempt), so the signature binds the nonce the MAC
  and the replay guard use: a captured signed command cannot be re-MACed
  under a fresh nonce by a holder of the provider key. The provider verifies
  it with the caller's PUBLIC key (`RPC_SERVICE_KEYS`) after the MAC and the
  allowlist and requires the signer to be the MAC's caller and `n` to be
  the request's nonce (`401 rpc_auth_signature_required` /
  `rpc_auth_invalid`). The provider never holds a caller's Ed25519 private
  key, so **a leaked provider X25519 key can forge reads at most, never
  effects**. A caller in `service_mac` signs every call that may write
  whenever it holds `RPC_PRIVATE_KEY`; startup refuses a calling service
  without one while the requirement is on.
- `all`: every call is signed and checked — reads become KCI-resistant too,
  at the Ed25519 price on every call (the ≈ 126 µs this ADR removes).
- **Residual risk (documented, accepted in the default):** with P's X25519
  key an attacker can issue **reads** to P as any caller, naming any tenant
  — i.e. read whatever P's read contracts expose. Treat a provider's X25519
  key like its database credentials; rotate it at once on suspicion;
  deployments that need KCI-resistant reads set `all`.
- Retired keys widen the window: a kid kept in `RPC_DH_EXTRA_PRIVATE_KEYS`
  but no longer published is reported at startup (warning; an error under
  `multi_tenant_strict`). The rotation overlap is bounded (below).

Alternatives considered for KCI: an interactive authenticated key exchange
with ephemeral keys on both sides (Noise KK/IK style) resists KCI for every
call but needs per-connection handshakes and session state across a pooled
HTTP client — a protocol of its own, not a MAC; per-caller provider keys
only shrink the blast radius per leaked key, they do not remove it (all of a
provider's keys live in the same secret store); signing every call is the
`all` option. Signatures on effects only keep the read path cheap and make
the worst case of a provider-key leak "reads", which the decision accepts.

### Ed25519 audience (closes review finding 1 for the migration window)

In `service_identity`, `dual` and `service_mac_dual` a caller whose binding
pins the provider (`service_id`) adds `aud` to its signed context; a provider
that finds `aud` ≠ its own id answers `401`. Old providers ignore the extra
field, old callers send none (accepted) — so it is rolling-upgrade safe.

### Key rotation

- `RPC_SERVICE_DH_KEYS[svc]` may list several kids. Select the addressed kid
  explicitly with `RPC_SERVICE_DH_CURRENT_KIDS={"<svc>": "<kid>"}`; JSON
  object insertion order is not a rotation protocol. Existing configurations
  without an explicit selection use the lexicographically greatest published
  kid, which is deterministic under JSON reordering but does **not** infer
  which arbitrary kid is newest. Set the explicit map before rotating.
- A provider accepts requests addressed to its current kid
  (`RPC_DH_KEY_ID`) and to any kid in `RPC_DH_EXTRA_PRIVATE_KEYS`
  (`{"<kid>": "<PEM>"}`, SECRETS_DIR file `rpc_dh_extra_private_keys`).
- Rotating provider P (no failed call at any step): 1) pin the existing kid1
  in `RPC_SERVICE_DH_CURRENT_KIDS[P]`, generate kid2, add its private key
  to P's `RPC_DH_EXTRA_PRIVATE_KEYS`; 2) publish kid2 in
  `RPC_SERVICE_DH_KEYS[P]` everywhere, then set
  `RPC_SERVICE_DH_CURRENT_KIDS[P]=kid2` (callers start addressing kid2; P
  accepts both); 3) make kid2 P's `RPC_DH_PRIVATE_KEY`/`RPC_DH_KEY_ID` and
  move kid1 to the extras; 4) remove kid1 from `RPC_SERVICE_DH_KEYS`
  everywhere; 5) after `RPC_AUTH_MAX_SKEW_SECONDS`, drop kid1 from P. A
  caller's own rotation needs only steps 2–5 (its kid travels in the header).
- **Bounded overlap.** Steps 1–5 take one rolling deploy each; the whole
  rotation should complete within 24 h, and step 5 must follow step 4 within
  one deploy: a kid still in `RPC_DH_EXTRA_PRIVATE_KEYS` after it left
  `RPC_SERVICE_DH_KEYS` keeps deriving every caller's key towards P (KCI,
  above) and is reported at every start (`multi_tenant_strict`: refused).
  `sandloom keys generate --purpose rpc-dh --service <id>` emits one X25519 pair.
- **Startup checks** (`Settings` validation, every MAC mode): own X25519 key
  present and its published half matches; every extra key parses, has a
  distinct kid, and (if published) matches; every binding without its own
  `internal_token` is pinned (`service_id`) to a service with published
  X25519 keys and **each pair key derives** (rejects low-order / all-zero
  points at startup); every explicit current kid is published and a selected
  local kid has its private key installed; no two services publish the same
  X25519 public key; the
  X25519 private key is not the Ed25519 signing seed; every published key is
  an X25519 key (the existing parse). `service_mac_dual` additionally needs
  the Ed25519 identity of `service_identity`.

### Errors

- Rejection log lines are rate-limited (one per reason and caller per
  minute); the counter keeps the exact rate.
- Provider: `401 rpc_auth_invalid` for every cryptographic / parse failure
  (malformed header, unknown kid, wrong audience, wrong key, bad tag, unknown
  version) — an unauthenticated party learns nothing about which check
  failed; `401 rpc_auth_expired` / `rpc_auth_replayed` are only reachable
  **after** the tag verified; `401 rpc_auth_required` (downgrade); `403
  rpc_caller_forbidden`; `503 rpc_replay_unavailable`. The detailed reason
  is a fixed vocabulary in `sandloom_rpc_auth_rejected_total{reason}` and a log
  line with ids and kids only — never a key, tag, or derived secret.
- Caller: every auth code (`rpc_auth_*`, `rpc_identity_*`,
  `rpc_caller_forbidden`, `rpc_replay_unavailable`) maps to `503
  RemoteServiceUnavailable` with its code (review finding 2); a bare 401/403
  without an envelope already did.

## Threat model

Assets: the right to invoke contract methods of a provider as a given caller
service (and to name a tenant in its context); integrity of arguments and
results. Out of scope: confidentiality (TLS/mTLS), compromise of the provider
process itself (it runs the contract), denial of service by an on-path party.

| Attacker | `service_identity` (Ed25519) | `service_mac` (pair MAC) |
|---|---|---|
| **Network attacker inside the cluster** (reads, injects, replays; holds no key) | cannot forge; **can replay a captured request to another service serving the same contract** within 300 s, and to the same provider within 300 s (no nonce cache) | cannot forge (no `K_req`); replay to another service fails (audience in key + MAC); replay to the same provider fails (nonce guard: per process, plus the shared layer for commands; reads across replicas — see "Replay") within a 60 s window |
| **Compromised peer service Q** (holds Q's keys) | can call what `RPC_ALLOWED_CALLS[Q]` allows; cannot sign as C | same; can derive only pair keys that involve Q: it can forge requests **Q→P** (allowed anyway) and, as a provider, "requests from C" **only to itself** (`K_req(C→Q)`), i.e. nothing it could not do by running its own code; cannot derive `K_req(C→P)` for any other P (needs `c` or `p`) |
| **Compromise of a static X25519 private key of a caller C** (leaked config / secret store) | — (the DH key only keyed responses) | **new exposure:** the holder can derive `K_req(C→P)` for every P and forge C's **reads** to all its peers (until C rotates); C's commands still need C's Ed25519 key |
| **Compromise of a static X25519 private key of a provider P** (KCI) | P's outbound rights only | **new exposure:** the holder derives `K_req(C→P)` for **every** caller C and can issue **reads** to P as any caller and tenant; **commands** are refused without the caller's Ed25519 signature (`RPC_COMMANDS_REQUIRE_SIGNATURE`, on in prod; `all` also protects reads). P's X25519 key = "all inbound read rights" |
| **Leaked public config** (`RPC_SERVICE_DH_KEYS`, allowlist) | nothing secret | nothing secret: public keys and ids only; kids are not secrets |
| **Leaked `INTERNAL_API_TOKEN`** | opens ops endpoints, not contracts | same; in `service_mac` the token never authenticates `/_sandloom/rpc` |
| **Replay** | 300 s window, no nonce cache | 60 s window (configurable), nonce guard, fresh nonce + tag per attempt |
| **Downgrade** (strip MAC / present Ed25519 or token) | n/a | `service_mac`: refused (`rpc_auth_required`); `service_mac_dual`: MAC present → MAC only; MAC stripped → Ed25519 (migration window only; audience-bound when pinned); callers never fall back |
| **Key confusion** (sign key as DH key, response key as request key, direction swap, caller/provider id swap) | n/a | separate settings and parsers per key type; seed reuse refused at startup; distinct HKDF salt + info per purpose and direction; ids and kids in role order inside info **and** MAC input; tests: swapped ids, reversed direction, response key as request key all fail |
| **Low-order / malicious public key in config** | n/a | `cryptography` refuses the exchange; all-zero `Z` refused explicitly; every configured pair derived at startup |

What changes for the worse, stated plainly:

1. **No third-party non-repudiation.** An Ed25519 signature proves to anyone
   holding C's public key that C produced the request; a MAC proves it only
   to P (both C and P can compute it). Nothing in Sandloom uses requests as
   evidence for a third party (audit records are written by the provider
   about an authenticated caller), so this property is not relied upon.
2. **A provider can forge requests from a caller — to itself only.** P can
   compute `K_req(C→P)`, so P could fabricate "C called me". P already
   executes whatever it likes; it cannot use the key towards any other
   service.
3. **The X25519 key becomes a signing-grade secret** (rows "Compromise of a
   static X25519 private key" above): before, its leak allowed forging
   responses of that service; now it also allows forging that service's
   read requests to every peer and — for a provider — any caller's read
   requests to it (KCI). Rotation is per service and needs no other service
   to change keys.
4. **Key-compromise impersonation of reads** (section above): accepted in
   the default; commands are protected by the caller's signature, and
   `RPC_COMMANDS_REQUIRE_SIGNATURE=all` removes it at the Ed25519 price.

What improves:

1. **Audience binding by construction** (review finding 1 closed in
   `service_mac`; in the Ed25519 modes for pinned bindings).
2. **Replay protection** beyond a time window (nonce guard).
3. **Cost.** Per **read** call ≈ 126 µs (sign + verify) → ≈ 11 µs on both
   sides together, replay guard and strict context decode included; a
   command keeps the Ed25519 cost (+11 % for the MAC) — see "Measurements".
4. **Error mapping** (review finding 2 closed in every mode).

## Measurements

`benchmarks/native/bench_rpc_auth.py` — one call's authentication work per
side, through the real Sandloom functions (context built outside the op in both
modes, ~100-byte msgpack body). Wall clock, CPython 3.12.10, arm64, cases
interleaved round-robin, n = 5000 × 9 reps, median (min);
`benchmarks/native/results/rpc_auth_wall_2026-10-21.txt`:

| side | Ed25519 (`service_identity`) | pair MAC (`service_mac`) | |
|---|---|---|---|
| caller: authenticate one request | 30.9 µs (`rpc_identity.sign`) | **3.1 µs** (key lookup + encoding + HMAC + header) | 10× |
| provider: verify one request | 95.1 µs (`rpc_identity.verify` + context decode) | **7.7 µs** (parse + key lookup + HMAC + compare + strict context decode + window + replay guard) | 12× |
| provider: tag verification alone | — | 3.8 µs | |
| **per call, both sides** | **≈ 126 µs** | **≈ 10.7 µs** | **−115 µs** |

Where the remaining µs go (cProfile): the strict context decode (duplicate-
key check, 2.6 µs), header regex, key-cache lookups; the HMAC itself is ≈ 1 µs
(the pair's fixed MAC-input prefix is absorbed into the prepared HMAC
context once per key). The shared nonce layer adds one security-cache `SET NX`
round trip for **commands only** (`RPC_REPLAY_GUARD=commands`), which already
pay a database transaction.

Cachegrind (instruction counts — the stand's evidence, insensitive to the
shared machine's load): `CG_K=1000 benchmarks/native/cg_micro.sh
bench_rpc_auth.py`, CPython 3.13.16 in the ladder's valgrind image,
Ir/op = (Ir(K) − Ir(0)) / K; `benchmarks/native/results/cg_rpc_auth.json`:

| case | Ed25519 (`service_identity`) | pair MAC, read | pair MAC, **command** (MAC + Ed25519, KCI mitigation) |
|---|---|---|---|
| caller | 250 149 Ir | **45 157 Ir** | 295 837 Ir |
| provider (full check) | 824 505 Ir | **121 612 Ir** | 896 281 Ir |
| provider, tag only | — | 60 001 Ir | — |
| **per call, both sides** | **1 074 654 Ir** | **166 769 Ir (6.4×, −908 k)** | **1 192 118 Ir (+11 % vs Ed25519)** |

Reads — the hot path (membership, claims, facts) — get the full saving.
A command pays the Ed25519 price plus the MAC (+118 k Ir, ≈ +15 µs wall:
`mac.cmd.caller` 47 µs / `mac.cmd.provider` 127 µs on a loaded machine,
`results/rpc_auth_wall_2026-10-21.txt`), on top of the database transaction
every command already runs — the price of "a leaked provider key never
forges an effect". `RPC_COMMANDS_REQUIRE_SIGNATURE=all` makes every call
cost the command column.

The provider's Ed25519 verify was ≈ 28 % of the provider side of an RPC
call (`R3t.srv` 2.51 M Ir in the research stand); a MAC-only read leaves
≈ 7 %.

## Consequences

- Split deployments move to `RPC_AUTH_MODE=service_mac`. Calling services
  keep their Ed25519 key (`RPC_PRIVATE_KEY`, public halves in
  `RPC_SERVICE_KEYS`) — it now signs only their commands. A pure provider
  needs only its X25519 key. Migration from `service_identity`: deploy this version everywhere (still
  `service_identity`; pinned callers now send `aud`) → every service holds
  its X25519 key, every binding pinned → `service_mac_dual` everywhere →
  `service_mac` everywhere. The first step is a hard
  precondition: a MAC-mode caller's keyed responses cover `X-SANDLOOM-RPC-Auth`,
  which a provider of an older version does not know (its keyed responses
  would fail `mac_mismatch`). The shipped
  microservices profile and the split conformance matrix run `service_mac`.
- `RPC_RESPONSE_BINDING` is unchanged and still covers responses (removed
  2026-10-25: responses are always keyed — see the amendment).
- The monolith is unaffected (contract calls are direct Python calls).

## Alternatives considered

- **Rust Ed25519 verifier** (`ed25519-dalek`): 2.1× wall, keeps
  non-repudiation, adds a native crypto dependency and keeps ~40 µs/call —
  rejected (research R2).
- **mTLS only**: authenticates the peer, but not the request at the
  application layer (a sidecar or gateway terminating TLS erases it) and
  gives no per-contract authorization input — complementary, not a
  replacement.
- **One shared HMAC key** (the `shared_token` model): any holder can
  impersonate every service — the property `service_identity` was introduced
  to remove.
- **Ephemeral DH per connection / Noise**: forward secrecy for a protocol
  that does not encrypt; much more code and state for no gain here.

## Security review

An adversarial review (a separate agent whose only brief was to break the
design and the code, 2026-10-21) produced eight findings; five were
confirmed with proof-of-concept tests. All are fixed, each with a regression
test (`test_rpc_pair_mac.py::test_r1…r8`), and the original PoCs now fail.

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | **high** (confirmed) | Response substitution in `service_mac` + strict: an on-path party drops request R1, then the victim's R2, and replays R1 with a SECOND `X-SANDLOOM-RPC-Nonce` (R2's). The fast-path binder read the last nonce, the verifier the first; the v2 response MAC covered only the Ed25519/context signatures (absent in `service_mac`), so the caller accepted R1's result as R2's | the response MAC input covers `X-SANDLOOM-RPC-Auth`; duplicated auth/nonce/context/signature headers are refused and never bound |
| 2 | medium (confirmed) | `service_mac_dual`: stripping the MAC fell back to Ed25519 — no nonce guard, 300 s window — for every request of the migration window | signed `"mac": 1` context flag; MAC-mode providers refuse a flagged context without its MAC |
| 3 | medium (confirmed) | A peer with a published X25519 key but no allowlist entry could fill the replay table (and the shared cache) and make the provider fail closed for everyone | authorize right after the tag, before freshness and the nonce guard |
| 4 | low | The shared-layer TTL was computed from one replica's clock | constant (now `3·skew`, second review) |
| 5 | low (confirmed) | `iat = 10**400` (authenticated caller) raised `OverflowError` → 500 | range-check `iat` before arithmetic → `rpc_auth_expired` |
| 6 | low | Unauthenticated junk produced one WARNING per request (log flooding) | rate-limited rejection logs |
| 7 | low (confirmed) | Shared replay keys collided for ids containing `:` | key = SHA-256 of the length-prefixed fields |
| 8 | info | Guard rebuilt (state lost) when two configs differed in size; ~100 MB default; ADR TTL wording | guard never rebuilt; default 200 000; wording fixed |

Claims the review could not break: forgery without `K_req` (swapped ids,
reversed direction, response key as request key, self-pairs, a compromised
third service); injectivity of the encoding and the single canonical header
form; nothing decoded before the tag verifies; audience binding (also the
Ed25519 `aud`); downgrade refusal in `service_mac` and no fallback once a MAC
is present; low-order / all-zero points refused at startup and verification;
cache keys follow key rotation; replay-guard window arithmetic; 503 mapping
at the caller.

Remaining, accepted: the endpoint answers `401 rpc_auth_invalid` (not the
hidden 404) to a malformed `X-SANDLOOM-RPC-Auth`, revealing the MAC mode to a
party that can already reach the internal network; reads replayed to a
different replica / worker within the window are served under
`RPC_REPLAY_GUARD=commands` (no effect, and the response is bound to the
original request, so it cannot be substituted for another call).

### Second review (independent, 2026-10-21)

A second adversarial review by a reviewer who had not seen the design found
no exploitable cryptographic or verification flaw, and five items, all fixed
(tests in `test_rpc_pair_mac.py`, section "second review"):

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | **medium** (confirmed with a script) | **Key-compromise impersonation**: the holder of a provider's X25519 private key derives every caller's pair key and can forge requests to it as the most privileged caller; the threat model only covered caller-key leaks | "Key-compromise impersonation" section; commands also need the caller's Ed25519 signature (`RPC_COMMANDS_REQUIRE_SIGNATURE`, on in prod, `off` refused under `multi_tenant_strict`, `all` for reads); stale extra kids warned / refused; bounded rotation overlap; tests pin that a forged read passes and a forged command is refused with no effect |
| 2 | low | Shared TTL `2·skew + 2` only holds for replica clocks within 2 s | `3·skew` (replica drift ≤ skew), docstring + test |
| 3 | low | `RPC_REPLAY_GUARD=local` allowed in prod; the fail-open shared layer was silent outside prod | refused in prod; startup warning outside prod |
| 4 | low | One allowed peer could fill the 200 k process table (→ 503 for everyone) | per-caller quota (`RPC_REPLAY_CACHE_PER_CALLER_MAX_ENTRIES`) |
| 5 | tests | Missing: KCI, `X-Tenant-Id` on the MAC path, an old provider kid from the extras after unpublishing, replica clock drift, `HEAD` on the status route | added; `HEAD` on `/_sandloom/rpc/<contract>` is now `405` (it was served under a `GET` MAC) |

### Third review (re-review of the KCI fix, 2026-10-21)

Verdict "safe to merge": the KCI mitigation holds (no bypass through a
missing / duplicated signature, a foreign signer, method-kind confusion or
a mode switch). Three low findings, fixed:

| # | Finding | Fix |
|---|---|---|
| 1 | The command signature did not cover the nonce: with a leaked provider key, a captured genuine signed command could be re-MACed under fresh nonces and pass authentication repeatedly in the window (only the durable command dedupe prevented a second effect) | the signed context carries the attempt's nonce (`n`), re-signed per attempt (the transport retry stays safe); the provider requires `n` = the MAC nonce. Tests: the exact re-MAC scenario (refused, no effect), a command retried after a lost answer (accepted, executed once). A per-call guard key was rejected: it would turn the safe retry of a command into a false replay |
| 2 | The per-caller quota (50 000) counted reads too: ≈ 800 calls/s per caller per process → `503` under normal load | default = half the table (100 000), sizing formula, alert hint |
| 3 | The shared TTL (`3·skew`) relies on replica drift ≤ skew, unchecked | explicit requirement in `deploy/README.md`; `sandloom_rpc_auth_clock_skew_total{caller}` + a rate-limited log for accepted requests off by more than half the window |

## Follow-ups

- `RPC_RESPONSE_BINDING=strict` still accepts `alg=token` response bindings
  for token-authenticated calls (`shared_token`, `dual`, a binding with its
  own `internal_token`) — inherent to the token model; drop it once `dual`
  is retired. Done 2026-10-25 (amendment): the token modes and `alg=token`
  bindings are gone.

## Amendment (2026-10-25)

Sandloom carries no backward compatibility before its first release, so the
migration path above is removed and the target state is the only state:

- `RPC_AUTH_MODE` (`shared_token`, `dual`, `service_identity`,
  `service_mac_dual`, `service_mac`) and `RPC_RESPONSE_BINDING`
  (`permissive`, `strict`) are removed settings
  (`sandloom.core.config.tiers.REMOVED`): startup refuses them unless set to their
  inert value (`service_mac` / `strict`).
- The pair MAC is the only internal-RPC request authentication (key from
  `RPC_DH_PRIVATE_KEY` / `RPC_SERVICE_DH_KEYS`, audience-bound, nonce replay
  guard `RPC_REPLAY_GUARD`, window `RPC_AUTH_MAX_SKEW_SECONDS`), plus the
  caller's Ed25519 signature on command calls (`RPC_PRIVATE_KEY` /
  `RPC_KEY_ID` / `RPC_SERVICE_KEYS`; `RPC_COMMANDS_REQUIRE_SIGNATURE`: `auto`
  = on in prod, off in dev/test; `all` signs reads too) and the
  deny-by-default allowlist `RPC_ALLOWED_CALLS`. Ed25519-only and token
  requests are refused; `INTERNAL_API_TOKEN` guards only the ops endpoints
  and never authenticates `/_sandloom/rpc` (nor does
  `INTERNAL_ENDPOINTS_OPEN_IN_DEV` open it).
- Every `SERVICE_BINDINGS` entry must pin `"service_id"`; a process with
  bindings must hold its own `RPC_DH_PRIVATE_KEY` (startup refuses
  otherwise); a provider without one refuses every RPC call; a monolith (no
  bindings) needs no RPC keys.
- Responses are always keyed (v2 binding, X25519 pair key, pinned provider):
  unbound, unkeyed (v1) and `alg=token` replies are never accepted. An
  unkeyed error answer — the provider could not authenticate the request —
  surfaces at the caller as `503 RemoteServiceUnavailable` carrying the
  provider's error code.

## Related reading

- [Service RPC guide](../guides/service-rpc.md)
- [Security model](../reference/security-model.md)
