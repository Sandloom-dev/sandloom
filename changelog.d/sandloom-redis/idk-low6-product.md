### Fixed

- **A `streams` DLQ id or cursor past the unsigned 64-bit limit is refused as `InvalidArgument`** (idk-low6-product). The check of the entry id accepted any number of up to twenty digits, so `99999999999999999999-0` passed it and Redis answered "Invalid stream ID", which the API then reported as a server error. Each part of `<ms>-<seq>` is bounded to `2**64 - 1`. The transport raises `InvalidArgument` (not retryable, a 422 from the admin API) for a malformed `replay_dlq` id or `list_dlq` cursor, and for one over that bound, before Redis sees it. No upgrade step.

### Changed

- **The hint of a cluster redirect is passed to the error's constructor** (idk-low6-product). The mapping of a `MOVED` / `ASK` reply to `Unavailable` set `hint` as an attribute, for kernels before 0.7.0a14. The floor is 0.7.0a14, whose `InfraError` takes `hint=`, so the dead fallback is gone. No change in behaviour.
