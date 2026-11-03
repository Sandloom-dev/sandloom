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

Docs: `docs/operate/rotate-keys.md` (why each step comes in its order, and
the failures it avoids). This skill is the procedure.

## JWT access tokens

- **Symmetric (HS256, the monolith).** `JWT_PREVIOUS_SECRETS` (a JSON list)
  verifies but never mints. The new secret enters it first, so a replica not
  yet switched accepts tokens a switched replica signs:
  1. Generate the new secret (`openssl rand -hex 32`), add it to
     `JWT_PREVIOUS_SECRETS` and roll every process.
  2. Make it `JWT_SECRET` and put the old one in `JWT_PREVIOUS_SECRETS` in its
     place; roll. If `JWT_KID` is set, give it a new value in the same step:
     a token whose `kid` equals `JWT_KID` is verified with `JWT_SECRET` only,
     so an unchanged kid rejects every token of the other secret.
  3. After `ACCESS_TOKEN_EXPIRE_MINUTES` + `JWT_LEEWAY_SECONDS`, empty
     `JWT_PREVIOUS_SECRETS` and roll.

  In prod, previous secrets must be as strong as the current one, and the
  list must not hold the current secret.
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

- **X25519 (pair-MAC, every service).** Service P, kid1 → kid2; one rolling
  deploy per step, "everywhere" = every service's shared settings:
  1. `sandloom keys generate --purpose rpc-dh --service P --kid kid2`.
     Everywhere: pin `RPC_SERVICE_DH_CURRENT_KIDS={"P": "kid1"}` and add
     kid2's public half to `RPC_SERVICE_DH_KEYS["P"]`. Callers still address
     kid1 (without a pin they address the greatest published kid).
  2. On P: add kid2's private key to `RPC_DH_EXTRA_PRIVATE_KEYS`.
  3. Everywhere: `RPC_SERVICE_DH_CURRENT_KIDS["P"]="kid2"`. P accepts both.
  4. On P: `RPC_DH_PRIVATE_KEY` = kid2's key, `RPC_DH_KEY_ID=kid2`; kid1 moves
     to `RPC_DH_EXTRA_PRIVATE_KEYS`.
  5. In ONE deploy: remove kid1 from `RPC_SERVICE_DH_KEYS` everywhere and from
     P's `RPC_DH_EXTRA_PRIVATE_KEYS`.

  No step leaves an extra key that is not published (startup warns about
  one; `multi_tenant_strict` refuses it). Finish within 24 h.
- **Ed25519 (command signatures, callers):**
  1. Add the next kid to `RPC_SERVICE_KEYS` everywhere.
  2. Switch that service's `RPC_PRIVATE_KEY` + `RPC_KEY_ID`.
  3. Remove the old kid.
- `sandloom keys allowlist` prints `RPC_ALLOWED_CALLS` from the bindings.
- A provider's X25519 key is an "all inbound rights" secret. On a leak,
  rotate it first.

## Other secrets

- **`TWOFA_ENCRYPTION_KEY`** is a comma list, newest first. Prepend the new
  key; new secrets use it, stored ones decrypt with any listed key. Nothing
  re-encrypts stored secrets: a secret moves to the new key only when its
  user sets up 2FA again, so keep old keys listed until then (a dropped key
  fails those users with `TwoFASecretDecryptionError`). It goes to every
  process that loads identity.
- **`INTERNAL_API_TOKEN` / `METRICS_TOKEN`:** rotate and redeploy every
  service together. They must differ from each other.
- **`JWT_SECRET` keys identity's emailed links** (in a monolith and, as the
  issuer-only secret, in a split deploy). The links have no previous-secret
  list: rotating it invalidates outstanding reset and verification links.
- **Database and cache passwords:** rotate at the server, then redeploy.

## After a compromise

1. Rotate the key as above, removing the old one immediately.
2. Then revoke sessions:
   `sandloom security revoke-sessions --all --apply --reason "<incident>"`. In
   prod, add `--confirm-production`.

Runbooks: `docs/advanced/security-model.md`, `docs/operate/operator-reference.md`
(incident response), `deploy/README.md` (variables).
