### Changed

- **A `celery` task-queue instance with no broker is refused at startup, and the message names the INFRA option** (idk-flat-urls-s0a). The build raises `MissingBrokerError` (a `ValueError`, `fatal_at_startup`) saying `celery taskqueue instance 'default' has no 'broker' option: set the INFRA option taskqueue.default.broker, e.g. INFRA='{...}'`; it no longer says the kernel fills the broker from `BROKER_URL`.
