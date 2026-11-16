### Changed

- **An app the conf check refuses never becomes `celery.current_app`** (idk-low5-product). The driver built its apps with `Celery(...)`, which makes the new app current at construction, so an app refused for a foreign broker or result backend stayed the process's current app. It now builds with `set_as_current=False` and calls `app.set_current()` once the check has passed, for the provider's app and for the importable one.
- **`redis>=5.2.1,<6` is a dependency of its own, and `hiredis` is an optional extra** (idk-low5-product). `celery[redis]` accepted any redis release; the bound is the one `sandloom-redis` has. `pip install sandloom-celery[hiredis]` adds the hiredis parser.
