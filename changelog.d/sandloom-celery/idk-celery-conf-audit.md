### Breaking changes

- **A `rediss://` url must state how it verifies the server** (idk-celery-conf-audit). Kombu connected such a broker with no certificate check and only logged a warning, while Celery's result backend already refused the same url. The `celery` taskqueue options now refuse a `rediss://` `broker` or `backend` without `ssl_cert_reqs`, and one that verifies the certificate without `ssl_check_hostname=true` (redis-py before 6 accepts any name of a trusted CA otherwise), naming the option. Upgrade: add `?ssl_cert_reqs=required&ssl_check_hostname=true` to the url, or `?ssl_cert_reqs=none` to state that the check is skipped.
- **An `sqs://` url with a host now uses HTTPS** (idk-celery-conf-audit). Kombu built `http://<host>` for it unless `is_secure` was set. The driver states `is_secure` in the transport options, and a config module may repeat it but not turn it off, so a plain-HTTP emulator is no longer reachable. A `predefined_queues` entry of a config module may carry only its queue `url` (an AWS https queue url) and the backoff keys: its own credentials, region and endpoint are refused.
- **A padded or empty `;` chunk of a broker or backend url is refused** (idk-celery-conf-audit). Kombu split a failover url on `;` without stripping, so a chunk such as ` redis://other:6379` slipped past the scheme check.
- **More result-backend keys are refused in a config module** (idk-celery-conf-audit). `dynamodb_endpoint_url`, `firestore_project`, `s3_base_path`, `gcs_base_path` and `azureblockblob_base_path` are refused. The Azure container and the Cosmos DB database and collection names are accepted only at their Celery defaults. State the endpoint in the `backend` url.

### Security

- **A named Celery instance no longer becomes `celery.current_app`** (idk-celery-conf-audit). Building `INFRA.taskqueue.aux` made its app the thread's current app, so a later unbound `@shared_task` `.delay()` published to the `aux` broker. Only the `default` instance, when it is the process-wide app, is made current. A worker process still takes the one app it runs as current, which is Celery's own rule and a known gap of the audit.
- **The conf check covers the rest of the result-backend connection keys and the SQS and sentinel transport options** (idk-celery-conf-audit). `database_url`, the MongoDB, Couchbase, ArangoDB, Cassandra, S3, GCS and cache backend settings, `sentinel_kwargs`, and the SQS `is_secure`, `port` and `sts_role_arn` are refused in a config module, like the keys the check already refused. `database_engine_options` and `broker_failover_strategy` are accepted only in a shape that cannot name another host. An `amqps://` broker verifies the certificate chain and not the hostname, which the driver cannot change.

### Added

- **`master_name` and `backend_master_name` options for a `sentinel://` url** (idk-celery-conf-audit). Kombu and Celery read the master only from the transport options, which the conf check refuses for a connection key, so a `sentinel://` broker could not connect. The master is now an `INFRA.taskqueue.<name>` option and a `sentinel://` url without it is refused. A config module may repeat the same master.

### Changed

- **The transport options a config module may carry depend on the scheme** (idk-celery-conf-audit). The SQS `region` and `queue_name_prefix`, the redis `retry_on_timeout`, `ack_emulation` and `unacked_*` keys, the amqp read and write timeouts, `broker_login_method` (a SASL mechanism, no route) and the result backend's `result_chord_ordered` are accepted. `design/infra-drivers/celery-conf-audit.md` lists every key with its decision.
