# idk-config-options: `SMTP_*`, `S3_*` and `EVENT_STREAM_*` become driver options (2026-11-15)

Infra-drivers design (ADR-0016): the smtp notifier, the s3 object store and the
`streams` event bus read their configuration from the options of their INFRA
instance. The flat settings are removed (`config/tiers.py` `REMOVED`).

## Changed entries

- G-WINDOW-03 (wording): the inbox-purge guard compares `--inbox-days` with the
  `retention_seconds` option of the `eventbus` instance, no longer with
  `EVENT_STREAM_RETENTION_SECONDS`. The refusal and its test
  (`test_guarantee_windows.py::test_inbox_purge_window`) do not change.
- G-EVENT-12 (wording): the trim bounds are the `retention_seconds` and `max_length`
  options of the `streams` instance, not the two settings.
