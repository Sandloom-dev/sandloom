# sandloom-standard

**The standard Sandloom composition.**

The composition root that every process of a standard Sandloom deployment
boots: the API, the Celery worker and beat, `sandloom migrate` and the operator
commands. It composes the module distributions installed beside it, so a
deployment that installs identity, tenancy and billing serves all three from
one root.

Part of [Sandloom](https://github.com/Sandloom-dev/sandloom), the Python backend
framework for AI-driven development.

## Install

```bash
python -m pip install --pre "sandloom[standard]"
```

`sandloom[standard]` installs this distribution with Redis, Celery and the
standard modules. Point every process at the root:

```bash
SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main
gunicorn -c python:sandloom.core.cli.gunicorn_conf sandloom_standard.main:app
```

The Sandloom image and the shipped compose profiles set it already.
`SANDLOOM_MODULES` still selects the modules a process loads. The kernel's
`sandloom.main` stays the lean root: installed modules, nothing else. An
application made with `sandloom new` keeps its own `<app>.asgi` root and does
not need this distribution.

This is alpha software. Review the compatibility policy before upgrading.

## Links

- [Source and issue tracker](https://github.com/Sandloom-dev/sandloom)
- [Documentation](https://sandloom.dev/docs/)
- [Compatibility policy](https://github.com/Sandloom-dev/sandloom/blob/main/docs/contributing/versioning.md)

## License

Apache-2.0. Copyright 2026 Vasyl Cherniavskyi.
See [LICENSE](https://github.com/Sandloom-dev/sandloom/blob/main/LICENSE) and [NOTICE](https://github.com/Sandloom-dev/sandloom/blob/main/NOTICE).
