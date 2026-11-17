### Breaking changes

- **The `celery` taskqueue driver refuses connections it used to accept** (idk-celery-conf-audit). A `rediss://` broker or backend url must state `ssl_cert_reqs` (and `ssl_check_hostname=true` when it verifies the certificate), a `sentinel://` url needs the `master_name` option, an `sqs://` url with a host uses HTTPS, and a padded `;` chunk or a result-backend endpoint set in a config module is refused. Upgrade: see [Upgrading the Celery driver's connection checks](docs/operate/upgrade.md#upgrading-the-celery-drivers-connection-checks).
