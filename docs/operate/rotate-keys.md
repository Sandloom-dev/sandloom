---
title: Rotate keys and secrets
description: Replace the JWT secret or signing key, a service's internal RPC keys, the 2FA encryption key and the ops tokens without signing users out or failing a call, and what changes after a leak.
type: how-to
job: When a key is due for rotation or may have leaked, I want to replace it without a forced logout or a failed service call, so the rotation is routine and a leak is contained.
purpose: The operator rotates each Sandloom key and secret in the order that keeps every token and call valid, checks each process before rolling it, and knows which waits a leak skips.
audience: [operator, agent]
level: operating
---

# Rotate keys and secrets

**Goal:** a new key or secret in place of an old one, with no user signed
out and no failed service call. What each key protects is in the
[Security model](../advanced/security-model.md).

**Use it** on a rotation schedule, when a person who held a key leaves, and
after a suspected leak ([After a key leaks](#after-a-key-leaks)).

**Do not use it** to set up keys for the first time: see
[Split into services](split-into-services.md#keys-and-running-it) and
[Secrets management](operator-reference.md#secrets-management).

Every rotation follows one order. Publish the new key next to the old one
everywhere, then switch the signer. Wait one lifetime of what the old key
signed, then remove the old key. A leaked key skips the wait.

## Prerequisites

- Your secret store feeds each process, for example as files in
  `SECRETS_DIR`. Keep private keys out of compose files.
- You can roll every service of the deployment, one setting change per
  rolling deploy.
- The lifetimes the waits depend on: `ACCESS_TOKEN_EXPIRE_MINUTES` (15 by
  default) and `JWT_LEEWAY_SECONDS` (10 by default).

`sandloom keys generate --out-dir <dir>` writes key files with mode 0600 and
never replaces one. Give each rotation a fresh directory:

```bash
sandloom keys generate --kid jwt-2 --out-dir rotation/jwt-2
```

It prints what it wrote and the settings that go with the key:

```text
wrote rotation/jwt-2/jwt_private_key (issuer only, 0600)
wrote rotation/jwt-2/jwt_public_keys (every service)
JWT_ALGORITHM=EdDSA
JWT_KID=jwt-2
```

Run again into the same directory, it stops with
`error: rotation/jwt-2/jwt_private_key exists; use a fresh rotation directory`.

## Rotate the JWT secret (HS256)

A monolith signs and verifies access tokens with one `JWT_SECRET`.
`JWT_PREVIOUS_SECRETS`, a JSON list, holds secrets that still verify tokens
but never sign one. The new secret enters that list first: during a rolling
deploy, a replica that has not switched yet must accept the tokens a
switched replica signs.

1. Generate the new secret: `openssl rand -hex 32`.
2. Add the new secret to `JWT_PREVIOUS_SECRETS` and roll the deployment.
   Every process accepts tokens signed with either secret.
3. Make the new secret `JWT_SECRET` and move the old one into
   `JWT_PREVIOUS_SECRETS` in its place. If you set `JWT_KID`, give it a new
   value too, then roll. A token whose `kid` equals `JWT_KID` is verified
   with `JWT_SECRET` alone: an unchanged kid would reject the tokens signed
   with the other secret.
4. After `ACCESS_TOKEN_EXPIRE_MINUTES` plus `JWT_LEEWAY_SECONDS`, empty
   `JWT_PREVIOUS_SECRETS` and roll again.

In production a previous secret must be as strong as `JWT_SECRET`, and the
list may never hold the current secret: startup refuses both.

`JWT_SECRET` also keys identity's emailed verification and password-reset
links, and those links have no previous-secret list. Links sent before the
switch stop working, and their users request new ones.

## Rotate the JWT signing key (EdDSA, RS256, ES256)

Between services, only the token issuer (the service that loads identity)
holds `JWT_PRIVATE_KEY`; every service verifies with `JWT_PUBLIC_KEYS`.

1. Generate the pair into a fresh directory, as in
   [Prerequisites](#prerequisites). Its `jwt_public_keys` file holds only
   the new kid.
2. Merge the new entry into `JWT_PUBLIC_KEYS` next to the current one
   (`{"jwt-1": "<PEM>", "jwt-2": "<PEM>"}`) and roll every service. Both
   kids verify.
3. Switch the issuer: `JWT_PRIVATE_KEY` is the new private key and
   `JWT_KID=jwt-2`. Roll the issuer.
4. After `ACCESS_TOKEN_EXPIRE_MINUTES` plus `JWT_LEEWAY_SECONDS`, remove
   `jwt-1` from `JWT_PUBLIC_KEYS` and roll every service.

Startup checks the issuer's pair. Switching before step 2 reached the
issuer fails with
`JWT_KID='jwt-2' is not in JWT_PUBLIC_KEYS: tokens minted with it would not verify anywhere`.

Refresh tokens are random values stored as hashes, and no key rotation
touches them. On the issuer, `JWT_SECRET` keys only the emailed links, with
the effect described for HS256.

## Rotate a service's RPC keys

Each service rotates its own RPC keys, and no other service changes its
keys meanwhile. Every service holds an X25519 key that authenticates its
requests and responses. A service that calls others also holds an Ed25519
key that signs its commands. [Service RPC](service-rpc.md) explains both.

### X25519 key

The steps rotate the key of a service `tenancy` from kid `tenancy-dh-1` to
`tenancy-dh-2`. Each step is one rolling deploy; "everywhere" means the
shared settings of every service.

1. Generate the key:
   `sandloom keys generate --purpose rpc-dh --service tenancy --kid tenancy-dh-2 --out-dir rotation/tenancy-dh-2`.
   Everywhere, pin the kid callers address with
   `RPC_SERVICE_DH_CURRENT_KIDS={"tenancy": "tenancy-dh-1"}`, and add the
   new public half to `RPC_SERVICE_DH_KEYS["tenancy"]`. Callers still
   address `tenancy-dh-1`.
2. On `tenancy`, add the new private key to `RPC_DH_EXTRA_PRIVATE_KEYS`
   (`{"tenancy-dh-2": "<PEM>"}`).
3. Everywhere, set `RPC_SERVICE_DH_CURRENT_KIDS["tenancy"]` to
   `tenancy-dh-2`. Callers address the new kid, and `tenancy` accepts both.
4. On `tenancy`, make the new key `RPC_DH_PRIVATE_KEY` with
   `RPC_DH_KEY_ID=tenancy-dh-2`, and move the old one into
   `RPC_DH_EXTRA_PRIVATE_KEYS`.
5. In one deploy, remove `tenancy-dh-1` from `RPC_SERVICE_DH_KEYS`
   everywhere and from `tenancy`'s `RPC_DH_EXTRA_PRIVATE_KEYS`.

Without the pin, callers address the published kid that sorts greatest,
whatever the order of the JSON. In this order no step leaves an extra key
that is not published. Such a key still lets its holder forge requests to
the service: startup warns about one, and
`PRODUCTION_PROFILE=multi_tenant_strict` refuses it. Finish the five steps
within 24 hours.

### Ed25519 key

1. Generate the service's keys:
   `sandloom keys generate --purpose rpc --service academy --kid academy-2 --out-dir rotation/academy-2`.
   It writes an X25519 pair too; this rotation uses only `rpc_private_key`
   and `rpc_service_keys.academy.json`.
2. Add the new public key to `RPC_SERVICE_KEYS["academy"]` everywhere and
   roll.
3. On `academy`, set `RPC_PRIVATE_KEY` to the new key and
   `RPC_KEY_ID=academy-2`, and roll it.
4. Remove the old kid from `RPC_SERVICE_KEYS` everywhere.

Providers verify a command with the kid the caller names. A caller whose
key is not published refuses to start, naming the missing
`RPC_SERVICE_KEYS` entry.

## Rotate the other secrets

`TWOFA_ENCRYPTION_KEY` is a comma-separated list, newest first. Put the new
key in front (`openssl rand -hex 32`) and give the list to every process
that loads identity: migrate, app, worker and beat. New 2FA secrets are
encrypted with the first key, and stored ones decrypt with any key in the
list. Sandloom does not re-encrypt stored secrets: a secret moves to the new
key only when its user sets up 2FA again. Keep the old keys listed until
then. A user whose secret no key decrypts fails 2FA with
`TwoFASecretDecryptionError`.

`INTERNAL_API_TOKEN` opens the ops endpoints (`/diagnostics`, `/contracts`,
`/metrics`, the `/health` details) and `METRICS_TOKEN` opens `/metrics`
alone. Each holds one value with no overlap list. Change it in the shared
settings, roll every service together, then give the new value to whatever
sends the `X-Internal-Token` header, such as the metrics scraper. In
production the two tokens must differ.

Database and cache passwords rotate at the server. Then update
`DATABASE_URL` or `CACHE_URL` and roll the processes that use them.

## After a key leaks

1. Rotate the leaked key as above, but remove the old key in the same
   deploy that switches the signer, without the wait. Tokens or calls signed
   with it stop verifying at once: users signed in with such a token sign
   in again.
2. Revoke the sessions issued before the rotation. Read the dry run, then
   apply it:

   ```bash
   sandloom security revoke-sessions --all --reason "<incident>"
   sandloom security revoke-sessions --all --apply --reason "<incident>"
   ```

   In production, add `--confirm-production` to the second command.

A provider's X25519 key lets its holder send reads to that provider as any
caller. Rotate that key first. The whole procedure, from containment to
the audit export, is the
[security incident runbook](operator-reference.md#security-incident-runbook-database-leak-credential-leak-key-compromise).

## Verify

Before you roll a process, run `sandloom doctor` with its new settings.
The `boot` check loads the key material. A private key that does not match
its published kid fails:

```text
[config]
  OK   settings — ENVIRONMENT=dev, PRODUCTION_PROFILE=standard
  FAIL boot — JWT_PUBLIC_KEYS['jwt-2'] is not the public half of JWT_PRIVATE_KEY
```

With the matching key, the same line reads
`OK   boot — key material and RPC configuration usable`. The doctor exits 0
when every check passes, 1 on a warning and 2 on a failure.

## Next steps

- The settings each key lives in: [Configuration](../reference/configuration.md).
- Why internal RPC uses two keys per service:
  [ADR-0004](../adr/0004-rpc-pair-mac-request-authentication.md).
