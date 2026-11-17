# Celery conf audit (idk-celery-conf-audit)

Base: main fe8d538c. Sources read: celery 5.6.3, kombu and redis-py of the locked
environment (`.venv/lib/python3*/site-packages`; paths below are relative to it).
The code that applies this audit is `packages/sandloom-celery/src/sandloom_celery/connection_policy.py`
(the allow-lists and the instance checks) and `worker.py` (the conf check).

## Threat model

`INFRA.taskqueue.<name>` states the connection of an instance whole: the `broker`
and `backend` urls, and for a `sentinel://` url the master. A Celery conf (a config
module, `celery --config`, the default loader's `celeryconfig.py`, environment
variables) is read next to them, and some of its keys win over the url. The driver
accepts a conf only if it moves neither the host, the credentials nor the TLS
verification of what INFRA states. Every table row below is one key that a library
reads and a decision:

- **refuse**: the conf check names the option (never a key value) and stops the
  process;
- **allow**: tuning that moves nothing;
- **INFRA option**: the value comes from `INFRA`, not from the conf.

## Decisions by task item

| item | decision | why |
|---|---|---|
| (1) `rediss://` without `ssl_cert_reqs` | refuse in `CeleryTaskQueueOptions` (broker and backend), both for the provider and the importable app | kombu defaults it to `CERT_NONE` and only logs (`kombu/connection.py:273`); Celery's own result backend refuses the same url (`celery/backends/redis.py:68`). One rule for both. An explicit `ssl_cert_reqs=none` stays allowed: the operator states it. |
| (2) `sentinel://` broker | INFRA options `master_name` and `backend_master_name`; `sentinel_kwargs` not supported | The option is 20 lines (validation plus a merge into the transport options). Dropping sentinel would remove a scheme the docs promised. `sentinel_kwargs` is refused: it names the sentinel nodes' host, credentials and TLS (`kombu/transport/redis.py:1469-1470`), and redis-py reuses the `socket_*` options for them when it is `None` (`redis/sentinel.py:237-241`), so the broker probe's timeouts already bound them. |
| (3) harmless keys refused | allow-list per scheme | SQS `region`, `queue_name_prefix` and `predefined_queues` (shape-checked), `result_chord_ordered`, `global_keyprefix`, `retry_policy` for backends. |
| (4) named instance becomes `current_app` | only the default instance, built from the process-wide app's connection, is made current | `celery.current_app` is thread-local with the module-level `default_app` as fallback (`celery/_state.py:100`); an unbound `@shared_task.delay()` publishes through it. A named instance, or a `default` instance whose urls differ from the process-wide app's, stays out. Other threads' unbound `.delay()` reach Celery's global `default_app`, as before. |

## Broker transport options (`broker_transport_options`)

Celery merges them over the connection: `celery/app/base.py:1042-1062`
(`_connection`: `broker_transport_options`, `broker_use_ssl` as `ssl=`,
`broker_login_method`, `broker_failover_strategy`).

| scheme | key | reader | decision | reason |
|---|---|---|---|---|
| any | `visibility_timeout`, `polling_interval` | kombu virtual transports | allow | timing |
| any | `retry_policy`, `max_retries`, `interval_start`, `interval_step`, `interval_max` | `kombu/connection.py` ensure | allow | reconnect pacing |
| redis, rediss, sentinel | `socket_timeout`, `socket_connect_timeout`, `socket_keepalive`, `socket_keepalive_options`, `max_connections`, `health_check_interval`, `retry_on_timeout`, `client_name` | `kombu/transport/redis.py:704` (`from_transport_options`) | allow | socket and pool tuning |
| redis, rediss, sentinel | `global_keyprefix`, `sep`, `fanout_prefix`, `fanout_patterns`, `priority_steps`, `queue_order_strategy`, `ack_emulation`, `unacked_*` | same | allow | key names and redelivery bookkeeping inside the instance the url names |
| redis, rediss, sentinel | any key not listed above, e.g. `ssl`, `host`, `port`, `password`, `username`, `db`, `credential_provider`, `sentinel_kwargs` | allow-list, not a deny-list | refuse | a key outside the list is refused by default, so a key a later kombu release reads cannot move the connection unseen |
| sentinel | `master_name` | `kombu/transport/redis.py:1441,1473-1477` | INFRA option | accepted in the conf only equal to `INFRA`'s `master_name`; merged under the conf's options |
| sentinel | `min_other_sentinels` | `kombu/transport/redis.py:1469` | allow | quorum check, no connection change |
| sentinel | `sentinel_kwargs` | `kombu/transport/redis.py:1470`, `redis/sentinel.py:231-241` | refuse | host, credentials, TLS of the sentinel nodes |
| amqp, amqps, pyamqp | `confirm_publish`, `read_timeout`, `write_timeout`, `socket_settings`, `channel_max`, `frame_max` | `kombu/transport/pyamqp.py:200` (laid over `Connection(**opts)`) | allow | socket and channel tuning |
| amqp, amqps, pyamqp | `host`, `userid`, `password`, `virtual_host`, `ssl`, `login_method`, `port` | same | refuse | they replace the url's connection or turn TLS verification off |
| sqs | `wait_time_seconds` | `kombu/transport/SQS.py` | allow | long polling |
| sqs | `region` | `kombu/transport/SQS.py:263,747` | allow, checked | an AWS region name (`[a-z]{2}(-[a-z]+)+-[0-9]{1,2}`): kombu builds the endpoint host from it |
| sqs | `queue_name_prefix` | `kombu/transport/SQS.py:375` | allow, checked | letters, digits, `-`, `_`, at most 64 |
| sqs | `predefined_queues` | `kombu/transport/SQS.py:297,769-778` | allow, checked | each entry carries only `url`, `access_key_id`, `secret_access_key`, `region`, `backoff_policy`, `backoff_tasks`; its `url` is an SQS queue url with a credential of the instance's own |
| sqs | `is_secure`, `port`, `sts_role_arn`, `sts_token_timeout`, client config | `kombu/transport/SQS.py:915-934` (the endpoint is built from the url's host and `is_secure`, `port`), `:770-821` | refuse | `is_secure` is the plain-text switch, `port` moves the endpoint, `sts_role_arn` names an assumed role |
| other schemes (`memory`, `sqla`, `filesystem`, ...) | generic keys only | each transport | allow generic, refuse the rest | unknown transports read arbitrary keys; none is audited |

Other conf keys of the connection (existing checks, unchanged by this task):
`broker_url`, `broker_read_url`, `broker_write_url`, `result_backend` must equal
INFRA; `broker_use_ssl` must be absent, `None` or `False` (`celery/app/base.py:1055`);
`broker_login_method` and the failover callable are refused;
`broker_failover_strategy` only `round-robin` or `shuffle`.

## Result backend

| key | reader | decision | reason |
|---|---|---|---|
| `result_backend_transport_options`: `retry_policy` | `celery/backends/redis.py:392-398` | allow | retry pacing |
| `result_backend_transport_options`: `result_chord_ordered` | `celery/backends/redis.py` | allow | chord ordering, no connection |
| `result_backend_transport_options`: `global_keyprefix` | `celery/backends/base.py:942-947` | allow | key prefix, any backend |
| `result_backend_transport_options`: redis tuning keys of the broker list | redis backend only | allow for a redis backend | ignored where unused |
| `result_backend_transport_options`: `master_name` | `celery/backends/redis.py:695-702` | INFRA option `backend_master_name` | same as the broker |
| `result_backend_transport_options`: `sentinel_kwargs` | `celery/backends/redis.py:682-687` | refuse | host, credentials, TLS of the sentinel nodes |
| `redis_host`, `redis_port`, `redis_db`, `redis_username`, `redis_password`, `redis_backend_use_ssl`, `redis_backend_credential_provider` | `celery/backends/redis.py` | refuse | each overrides a part of the url |
| `database_url`, `mongodb_backend_settings`, `couchbase_backend_settings`, `arangodb_backend_settings`, `cassandra_*` (servers, bundle, port, keyspace, table, auth provider, auth kwargs, options), `s3_endpoint_url`, `s3_region`, `s3_access_key_id`, `s3_secret_access_key`, `s3_bucket`, `gcs_bucket`, `gcs_project`, `cache_backend`, `cache_backend_options` | `celery/backends/*` | refuse | INFRA states the whole backend in its url; any of these names a host, a credential or a bucket |
| `database_engine_options` | SQLAlchemy backend `create_engine` | allow only `pool_pre_ping`, `pool_recycle`, `pool_size`, `max_overflow`, `pool_timeout` | `connect_args` and `creator` reach the DBAPI connect call |

## Left out, on purpose

- `AWS_ENDPOINT_URL*` and the other AWS environment variables are read by botocore
  for an SQS broker, outside the Celery conf: they are an operator's process
  environment, not a conf a project ships.
- `sentinel_kwargs` is not an INFRA option. A deployment whose sentinel nodes need a
  password or TLS of their own has no way to state it yet; the option can be added
  once there is a case, with its own audit.
- `broker_probe.py` needs no change: the probe builds its connection from the
  same url, and the sentinel nodes take its socket timeouts (`redis/sentinel.py:237-241`).
- An explicit `ssl_cert_reqs=none` in a `rediss://` url stays allowed: the operator
  decided, and kombu logs it.
