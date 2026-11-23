### Changed

- **A test run on a Redis with too few database indexes says what to start** (atomic-store-sqlite-flake). A stock `redis:7` has 16 indexes and the xdist layout puts worker N at the base index plus 16 x (N + 1); the claim of such an index aborted with redis-py's bare `DB index is out of range`. `claim_redis` now raises `TestResourceOwnershipError` naming the Redis (host, port, index; never a password) and the `--databases 256` option. Any other server reply still raises as it did. The required count is in `docs/reference/testing.md`.
