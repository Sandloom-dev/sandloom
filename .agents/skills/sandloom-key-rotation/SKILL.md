---
name: sandloom-key-rotation
description: Rotate Sandloom secrets and keys without a forced logout or an RPC outage — JWT (HS256 and EdDSA/RS256/ES256 kids), internal RPC X25519 and Ed25519 keys, TWOFA_ENCRYPTION_KEY, INTERNAL_API_TOKEN / METRICS_TOKEN — and what changes when a key is compromised. Use when asked to rotate, roll, renew or replace any of these, or after a suspected key leak.
---

# Rotate keys and secrets

Every rotation works the same way: **publish the new key next to the old one
everywhere, switch the signer, wait one lifetime, remove the old key.** A
compromised key skips the wait and is removed at once, which logs users out
or fails calls signed with it.

Generate keys with `uv run sandloom keys generate …`. Put private keys in the
secret store (`SECRETS_DIR` files), never in compose files.

## JWT access tokens

- **Symmetric (HS256, the monolith):**
  1. Move the current secret into `JWT_PREVIOUS_SECRETS` (a JSON list, still
     verified, never used to mint).
  2. Set the new `JWT_SECRET`. Optionally set `JWT_KID`.
  3. Drop the old secret after `ACCESS_TOKEN_EXPIRE_MINUTES`.

  In prod, previous secrets must be as strong as the current one.
- **Asymmetric (microservices):**
  1. Run `sandloom keys generate --kid jwt-2`. The private key goes to the issuer
     (identity) only.
  2. Add `jwt-2` to `JWT_PUBLIC_KEYS` **in every service** and roll them all.
  3. Switch identity to `JWT_PRIVATE_KEY=<new>` and `JWT_KID=jwt-2`. Startup
     checks the pair.
  4. After `ACCESS_TOKEN_EXPIRE_MINUTES` + `JWT_LEEWAY_SECONDS`, remove
     `jwt-1` everywhere.

  Refresh tokens are opaque and unaffected.

## Internal RPC keys (per service, independent; ADR-0004)

- **X25519 (pair-MAC, every service):**
  1. Publish the next kid in `RPC_SERVICE_DH_KEYS` everywhere.
  2. Switch `RPC_DH_PRIVATE_KEY` + `RPC_DH_KEY_ID`. The old private key may
     stay in `RPC_DH_EXTRA_PRIVATE_KEYS` until callers address the new kid.
  3. Drop the old kid within 24 h. Startup warns about leftovers, and
     `multi_tenant_strict` refuses them.
- **Ed25519 (command signatures, callers):**
  1. Add the next kid to `RPC_SERVICE_KEYS` everywhere.
  2. Switch that service's `RPC_PRIVATE_KEY` + `RPC_KEY_ID`.
  3. Remove the old kid.
- `sandloom keys allowlist` prints `RPC_ALLOWED_CALLS` from the bindings.
- A provider's X25519 key is an "all inbound rights" secret. On a leak,
  rotate it first.

## Other secrets

- **`TWOFA_ENCRYPTION_KEY`** is a comma list, newest first. Prepend the new
  key and keep the old ones until every TOTP secret is re-encrypted. It goes
  to every process that loads identity.
- **`INTERNAL_API_TOKEN` / `METRICS_TOKEN`:** rotate and redeploy every
  service together. They must differ from each other.
- **`JWT_SECRET` in a split deploy** is issuer-only: it keys identity's
  emailed links, so rotating it invalidates outstanding reset and
  verification links.
- **Database and cache passwords:** rotate at the server, then redeploy.

## After a compromise

1. Rotate the key as above, removing the old one immediately.
2. Then revoke sessions:
   `sandloom security revoke-sessions --all --apply --reason "<incident>"`. In
   prod, add `--confirm-production`.

Runbooks: `docs/reference/security-model.md`, `docs/operate/operator-reference.md`
(incident response), `deploy/README.md` (variables).
