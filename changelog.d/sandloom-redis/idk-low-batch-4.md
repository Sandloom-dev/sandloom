### Changed

- **A `MOVED` / `ASK` reply maps to an `Unavailable` that carries a hint** (idk-low-batch-4). The error has `hint` "cluster redirect: is the client cluster-aware?" (the sentence only, never the address or slot), which the outbox relay prints in its outage warning. The class and `retryable` are unchanged. Needs a `sandloom` release whose `InfraError` accepts `hint`.
