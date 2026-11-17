### Added

- **The Celery task queue implements `backup_hint`** (redis-out-runtime). A broker on a `redis://` or `rediss://` url reports its AOF advice and the note that a lost broker loses queued tasks, as the kernel did before. Other brokers report nothing.
