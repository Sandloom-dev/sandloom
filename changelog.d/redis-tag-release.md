### Changed

- **`sandloom-redis` releases by tag** (redis-tag-release). Its PyPI Trusted Publisher and the `pypi` environment's tag pattern `sandloom-redis-v*` are set up, so `tag_release` is true in `tools/packaging/packages.json`. A version bump of the kernel and `sandloom-redis` together still goes out through a manual run with `all`: each requires the other, and the plan refuses either tag while PyPI has neither at that version.
