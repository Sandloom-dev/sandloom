### Security

- **A `rediss://` url without `ssl_cert_reqs` is refused** (idk-celery-conf-audit). Kombu connected such a broker with no certificate check and only logged a warning, while Celery's result backend already refused the same url. The `celery` taskqueue options now refuse it for the `broker` and the `backend`, naming the option. Upgrade: add `?ssl_cert_reqs=required` to the url, or `?ssl_cert_reqs=none` to state that the check is skipped.
- **A named Celery instance no longer becomes `celery.current_app`** (idk-celery-conf-audit). Building `INFRA.taskqueue.aux` made its app the thread's current app, so a later unbound `@shared_task` `.delay()` published to the `aux` broker. Only the `default` instance, when it is the process-wide app, is made current.
- **The conf check covers the rest of the result-backend connection keys and the SQS and sentinel transport options** (idk-celery-conf-audit). `database_url`, the MongoDB, Couchbase, ArangoDB, Cassandra, S3, GCS and cache backend settings, `sentinel_kwargs`, and the SQS `is_secure`, `port` and `sts_role_arn` are refused in a config module, like the keys the check already refused. `database_engine_options` and `broker_failover_strategy` are accepted only in a shape that cannot name another host.

### Added

- **`master_name` and `backend_master_name` options for a `sentinel://` url** (idk-celery-conf-audit). Kombu and Celery read the master only from the transport options, which the conf check refuses for a connection key, so a `sentinel://` broker could not connect. The master is now an `INFRA.taskqueue.<name>` option and a `sentinel://` url without it is refused.

### Changed

- **The transport options a config module may carry depend on the scheme** (idk-celery-conf-audit). The SQS `region`, `queue_name_prefix` and `predefined_queues`, the redis `retry_on_timeout`, `ack_emulation` and `unacked_*` keys, the amqp read and write timeouts, and the result backend's `result_chord_ordered` are accepted. `design/infra-drivers/celery-conf-audit.md` lists every key with its decision.
