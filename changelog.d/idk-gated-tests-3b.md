### Changed

- **The kernel suite runs without Redis and Celery, to the last test** (idk-gated-tests-3b). The no-Redis pending list of kernel tests is empty and its ceiling is 0. Tests that checked a Redis or Celery driver moved to `packages/sandloom-redis/tests` and `packages/sandloom-celery/tests`; the kernel's own rules run on the `memory` drivers. A child process of a test selects the `memory` drivers where `sandloom-redis` is not installed. No upgrade steps: only tests changed.
