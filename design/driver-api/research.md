# Driver API — research

Status: **design research; no implementation**. Sources retrieved 2026-10-31.
Sandloom baseline: `1152ee989bc02641629747b9b1992f319b39c09c` (branch `claude/docs-overhaul`).
Links to source code point at the default branch as it stood on that date. Django docs are 5.2, JDK docs are 21.

Text marked **Inference** or **Judgement** is mine. Everything else reports what the linked source says.
Long pages were read by targeted search, not end to end; see [What I could not verify](#what-i-could-not-verify).

## Summary

- **Separate the caller's type from the implementer's type.** Go, OpenTelemetry and SLF4J all do. They are two promises, because a new abstract method does not break a caller but does break an implementer.
- **Resolve drivers by name, lazily.** SQLAlchemy and Sphinx load only what configuration selects. That removes load order, and a broken unselected plugin stops mattering.
- **Conflicts are errors unless an override is explicit.** Go, Sphinx, fsspec and stevedore's `DriverManager` all refuse by default.
- **A label does not hold.** PEP 411 records that a documented "provisional" status did not stop reliance. The mechanisms with teeth are compiler opt-ins and runtime refusals.
- **`type[T]` cannot carry an abstract contract under mypy.** A generic typed key can, and Sandloom already has one (`ContractRef[P]`).

## 1. Caller API versus implementer SPI

**Short answer.** Separate namespaces are the norm among the designs built for third-party drivers. JDBC is the main counterexample, and Kotlin and Rust show that one type can also carry two promises when the language enforces it.

- **Go.** `database/sql` gives users concrete types and `database/sql/driver` holds interfaces. The design note has user code call the `sql` package, which calls the driver interfaces, and keeps optional driver interfaces known only to the `sql` package ([doc.txt](https://github.com/golang/go/blob/master/src/database/sql/doc.txt)). The driver package opens by sending most code to `database/sql` ([driver.go](https://github.com/golang/go/blob/master/src/database/sql/driver/driver.go)).
- **OpenTelemetry.** API packages are what third-party libraries and application code import; the SDK holds constructors and plugin interfaces, and instrumentation authors are forbidden (MUST NOT) from referencing any SDK package ([overview](https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/overview.md)). Plugin interfaces may gain methods without a major bump only if existing implementers keep working, for example through default implementations ([versioning](https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/versioning-and-stability.md)).
- **SLF4J.** Callers use `org.slf4j`; a provider implements [`org.slf4j.spi.SLF4JServiceProvider`](https://github.com/qos-ch/slf4j/blob/master/slf4j-api/src/main/java/org/slf4j/spi/SLF4JServiceProvider.java).
- **Django.** Backends subclass a dedicated base, such as `BaseEmailBackend` in `django.core.mail.backends.base` ([email](https://docs.djangoproject.com/en/5.2/topics/email/#defining-a-custom-email-backend)). Cache authors are pointed to the built-in backends as reference implementations ([cache](https://docs.djangoproject.com/en/5.2/topics/cache/#using-a-custom-cache-backend)).
- **SQLAlchemy.** `Dialect` is the interface, but a note tells third-party dialects to subclass `DefaultDialect` instead ([interfaces.py](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/engine/interfaces.py)). The implementer base carries defaults, so the interface can grow.
- **JDBC, the counterexample.** One type serves both sides: vendors implement [`DatabaseMetaData`](https://docs.oracle.com/en/java/javase/21/docs/api/java.sql/java/sql/DatabaseMetaData.html) and users read capabilities from it.
- **Same type, two promises.** Kotlin's [`SubclassOptInRequired`](https://kotlinlang.org/api/core/kotlin-stdlib/kotlin/-subclass-opt-in-required/) exists for API that is stable to call and unstable to implement. Rust's [sealed-trait pattern](https://rust-lang.github.io/api-guidelines/future-proofing.html) closes a trait to outside implementers so methods can be added in non-breaking releases.

**Inference.** The asymmetry is the reason to split. Go's driver docs describe an interface that has evolved, and it grows by adding optional interfaces. OpenTelemetry's rule and Kotlin's annotation address the same problem.

## 2. Conformance and test kits

**Short answer.** There are three shipping forms. The capability gate is best when it reads the driver's own declaration, as CSI and Django do.

| Form | Example | How it runs | How capabilities gate it |
| --- | --- | --- | --- |
| Function returning an error | [`fstest.TestFS`](https://github.com/golang/go/blob/master/src/testing/fstest/testfs.go), [`iotest.TestReader`](https://github.com/golang/go/blob/master/src/testing/iotest/reader.go) | One call inside the author's test | Type detection: `TestReader` also checks `ReaderAt` or `Seeker` if implemented |
| Importable or subclassed suite | [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy/blob/main/README.dialects.rst), [pandas](https://pandas.pydata.org/docs/development/extending.html#testing-extension-arrays), [fsspec](https://github.com/fsspec/filesystem_spec/blob/master/fsspec/tests/abstract/__init__.py) | `from sqlalchemy.testing.suite import *`; pandas: subclass `base.BaseConstructorsTests` and provide fixtures | SQLAlchemy: a `Requirements` subclass answers `exclusions.open()` or `closed()` |
| Library and binary | [`csi-sanity`](https://github.com/kubernetes-csi/csi-test/blob/master/pkg/sanity/README.md) | `sanity.Test(t, config)` or a CLI against an endpoint | Asks the plugin, then [`Skip`s](https://github.com/kubernetes-csi/csi-test/blob/master/pkg/sanity/controller.go) if `isControllerCapabilitySupported` is false |

- **Declaration drift.** SQLAlchemy's `README.dialects.rst` has authors state capabilities in a separate `requirements.py`, on top of the dialect's own attributes. Some default rules do read the dialect, for example `config.db.dialect.insert_returning` ([requirements.py](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/testing/requirements.py)). Django's [`skipUnlessDBFeature`](https://github.com/django/django/blob/main/django/test/testcases.py) reads `connection.features` directly.
- **Expected failures as data.** Django's [`BaseDatabaseFeatures`](https://github.com/django/django/blob/main/django/db/backends/base/features.py) has `django_test_expected_failures` and `django_test_skips`, applied by [`mark_expected_failures_and_skips`](https://github.com/django/django/blob/main/django/db/backends/base/creation.py). SQLAlchemy lets a dialect override a suite test.
- **The kit moves with the framework.** New SQLAlchemy releases add tests and requirements, and the dialect maintainer is expected to track them ([README.dialects.rst](https://github.com/sqlalchemy/sqlalchemy/blob/main/README.dialects.rst)).
- **Kits have a stated scope.** `csi-sanity` is described as additional to a driver's unit, functional and e2e tests ([csi-test](https://github.com/kubernetes-csi/csi-test/blob/master/README.md)).
- **Self-attestation.** [`Driver.jdbcCompliant`](https://docs.oracle.com/en/java/javase/21/docs/api/java.sql/java/sql/Driver.html) may return true only if the driver passes the JDBC compliance tests. Nothing in that text verifies the claim.
- **No kit in these sources.** OpenTelemetry publishes a [compliance matrix](https://github.com/open-telemetry/opentelemetry-specification/blob/main/spec-compliance-matrix.md), a table rather than a test. Terraform's [testing docs](https://developer.hashicorp.com/terraform/plugin/testing) describe acceptance and unit tests for a provider's own behaviour. Django's docs offer third-party cache and email authors reference implementations, and I found no third-party kit.

**Inference.** A capability-gated skip proves nothing about a driver that under-declares. The kit should report what it skipped, and something must check that declarations are true (§4).

## 3. Discovery, ordering, conflicts and failure

**Ordering.** Python gives no guarantee.

- The [entry points spec](https://packaging.python.org/en/latest/specifications/entry-points/) leaves same-name conflicts between distributions to the consumer.
- I found no ordering statement in the [`importlib.metadata` docs](https://docs.python.org/3/library/importlib.metadata.html).
- **Inference from source.** Order follows `sys.path` and then directory listing, with the first distribution of a normalized name winning ([`entry_points`, `_unique`, `FastPath.children`](https://github.com/python/cpython/blob/main/Lib/importlib/metadata/__init__.py)). [`os.listdir`](https://docs.python.org/3/library/os.html#os.listdir) is documented as arbitrary order.
- Java is similar: module order within a layer is explicitly undefined ([ServiceLoader](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/ServiceLoader.html)).
- [pluggy](https://pluggy.readthedocs.io/en/stable/) calls hooks in LIFO registered order, with `tryfirst` and `trylast`. Its [loader](https://github.com/pytest-dev/pluggy/blob/main/src/pluggy/_manager.py) iterates distributions without sorting.

**Loading by name avoids ordering altogether.**

- [SQLAlchemy's `PluginLoader.load`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/util/langhelpers.py) tries in-process registrations, then built-ins, then the first matching entry point, and imports only that one. The name comes from the URL: `access.pyodbc` serves `access+pyodbc://`.
- [Sphinx's `preload_builder`](https://github.com/sphinx-doc/sphinx/blob/master/sphinx/registry.py) looks up one builder by name.
- Django imports the path named in settings ([`load_backend`](https://github.com/django/django/blob/main/django/db/utils.py), [`import_string`](https://github.com/django/django/blob/main/django/utils/module_loading.py)).

**Conflicts and overriding built-ins.**

| System | Same name twice | Overriding a built-in |
| --- | --- | --- |
| [Go `sql.Register`](https://github.com/golang/go/blob/master/src/database/sql/sql.go) | Panics | Not possible |
| [Sphinx](https://www.sphinx-doc.org/en/master/extdev/appapi.html) | `ExtensionError` | `override=True` |
| [fsspec `register_implementation`](https://github.com/fsspec/filesystem_spec/blob/master/fsspec/registry.py) | `ValueError` | `clobber=True`; but [its entry-point loader](https://github.com/fsspec/filesystem_spec/blob/master/fsspec/__init__.py) passes `clobber=True` and keeps the first of a duplicate name |
| [stevedore `DriverManager`](https://github.com/openstack/stevedore/blob/master/stevedore/driver.py) | `MultipleMatches` by default; resolver is pluggable | Not applicable |
| [pluggy `register`](https://github.com/pytest-dev/pluggy/blob/main/src/pluggy/_manager.py) | `ValueError`; the entry-point loader skips an already registered name | `set_blocked`; pytest `-p no:name` blocks even built-ins ([pytest](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)) |
| [SQLAlchemy](https://docs.sqlalchemy.org/en/20/core/connections.html#registering-new-dialects) | First hit | `registry.register()` in-process |
| [SLF4J](https://www.slf4j.org/codes.html) | Warns and uses the first | `slf4j.provider` system property ([LoggerFactory](https://github.com/qos-ch/slf4j/blob/master/slf4j-api/src/main/java/org/slf4j/LoggerFactory.java)) |

**Failure handling splits by whether the plugin was asked for.** (In this list, `DriverManager` under JDBC is `java.sql.DriverManager`, not stevedore's class.)

- **Fail fast.** [pytest](https://github.com/pytest-dev/pytest/blob/main/src/_pytest/config/__init__.py) raises `PluginImportFailure`, and its comment treats an unloadable installed plugin as a defect in that plugin. [`required_plugins`](https://github.com/pytest-dev/pytest/blob/main/src/_pytest/config/__init__.py) also checks versions. pluggy lets `ep.load()` errors propagate. [stevedore's `DriverManager`](https://github.com/openstack/stevedore/blob/master/stevedore/driver.py) re-raises by default. `ServiceLoader` throws `ServiceConfigurationError`.
- **Log and continue.** [stevedore's `ExtensionManager`](https://github.com/openstack/stevedore/blob/master/stevedore/extension.py), used where many extensions load, logs an error and moves on. [`DriverManager`](https://github.com/openjdk/jdk/blob/master/src/java.sql/share/classes/java/sql/DriverManager.java) in JDBC catches `Throwable` in an empty handler. SLF4J falls back to a no-op logger with a warning.

**Multi-phase registration.**

- pluggy separates specs from implementations, and `check_pending` raises `PluginValidationError` for implementations with no spec ([docs](https://pluggy.readthedocs.io/en/stable/#enforcing-spec-validation)). Historic hooks replay earlier calls to late registrants.
- pytest documents an [ordered startup](https://docs.pytest.org/en/stable/how-to/writing_plugins.html#plugin-discovery-order-at-tool-startup): blocks, built-ins, `-p`, entry points, `PYTEST_PLUGINS`, conftest files.
- Terraform negotiates a major version in the [handshake](https://github.com/hashicorp/terraform/blob/main/docs/plugin-protocol/README.md). CSI's [spec](https://github.com/container-storage-interface/spec/blob/master/spec.md) makes `GetPluginCapabilities` a required RPC.

## 4. Declaring optional capabilities

**Short answer.** Sources fall into three models. Each has one place to look, and none I read cross-checks a static declaration against runtime behaviour automatically. That check would have to be built.

- **The type is the declaration.** Go's optional interfaces are detected by assertion, with `ErrSkip` to fall back as if a method were absent ([driver.go](https://github.com/golang/go/blob/master/src/database/sql/driver/driver.go)). Terraform's framework uses [`ResourceWithImportState`](https://github.com/hashicorp/terraform-plugin-framework/blob/main/resource/resource.go) and siblings.
- **A class declares it, optionally refined at runtime.** SQLAlchemy dialects have `supports_*` attributes, and [`initialize(connection)`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/engine/interfaces.py) may adjust options from the server version. Django's [`BaseDatabaseFeatures`](https://github.com/django/django/blob/main/django/db/backends/base/features.py) mixes attributes with `cached_property` values. PEP 249 puts `apilevel`, `threadsafety` and `paramstyle` [at module level](https://peps.python.org/pep-0249/#globals), readable before connecting.
- **The plugin answers a query.** CSI's `GetPluginCapabilities` must return the same set from every instance of a plugin version ([spec](https://github.com/container-storage-interface/spec/blob/master/spec.md)), so it is runtime in transport and static in meaning. Terraform's [`ServerCapabilities`](https://github.com/hashicorp/terraform/blob/main/docs/plugin-protocol/tfplugin6.proto) exists for changes that cannot be detected directly.

**Static, before building.** `ServiceLoader` lets an application inspect a provider's type [before instantiating it](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/ServiceLoader.html). Django's [`required_db_features`](https://docs.djangoproject.com/en/5.2/ref/models/options/#required-db-features) is a consumer declaration, but it skips the model rather than failing.

**Mismatch detection is at call time or in the kit.** CSI returns `UNIMPLEMENTED`, and the caller must not retry. JDBC defines `SQLFeatureNotSupportedException`. `csi-sanity` exercises what the driver claims.

## 5. Typed access from Python

**Short answer.** Use a generic key object. Do not pass the contract class as `type[T]`.

- **The `type[T]` trap.** The typing spec lets parameters annotated `type[Proto]` accept only concrete, non-protocol subtypes ([protocols](https://typing.python.org/en/latest/spec/protocol.html)). mypy reports [`type-abstract`](https://mypy.readthedocs.io/en/stable/error_code_list.html#safe-handling-of-abstract-type-object-types-type-abstract) when an abstract class is passed where `type[T]` is expected.
  - **Tested with mypy 2.4.1 `--strict`:** `by_type(AbstractBase)` and `by_type(SomeProtocol)` both give a `type-abstract` error. pyright reported no error on the same file. A `Ref[T]` key reveals the right type in both.
  - **Tested, undocumented:** `def __init__(self, name, proto: type[P] | None)` called with a Protocol passes mypy. This is the shape of Sandloom's `ContractRef`, so it may rest on a checker gap. Do not depend on it for new code.
- **The key-object precedent.** [`pytest.StashKey[T]`](https://github.com/pytest-dev/pytest/blob/main/src/_pytest/stash.py) backs a type-safe mapping with mixed value types, and [`aiohttp.web.AppKey`](https://docs.aiohttp.org/en/stable/web_advanced.html) is recommended over string keys.
- **SQLAlchemy 2.** [`inspect()`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/inspection.py) uses overloads over generic protocols and falls back to `Any`. [`Session.get`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/orm/session.py) takes `_EntityBindKey[_O]` and returns `Optional[_O]`; entities are concrete, so the trap does not arise. [`create_engine`](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/engine/create.py) is overloaded. DB-API objects are [`Protocol`s](https://github.com/sqlalchemy/sqlalchemy/blob/main/lib/sqlalchemy/engine/interfaces.py), while `Dialect` is nominal.
- **Protocol or ABC.** PEP 544 keeps explicit subclassing available ([PEP 544](https://peps.python.org/pep-0544/#explicitly-declaring-implementation)). `@runtime_checkable` checks that members exist, not their signatures ([typing](https://docs.python.org/3/library/typing.html#typing.runtime_checkable)). mypy notes that explicitly subclassing a protocol allows an abstract-instantiation error to catch a gap ([mypy](https://mypy.readthedocs.io/en/stable/protocols.html)).
- **`Any` in returns.** The typing guide's [Any Trick](https://typing.python.org/en/latest/guides/writing_stubs.html#the-any-trick) justifies a partial `Any` for a value that can be `None`. It does not cover a function whose whole return is `Any`.
- **`TypeForm`.** [PEP 747](https://peps.python.org/pep-0747/) (Final) was written for functions that accept type forms, and its motivation lists `trycast(MyProtocolClass, obj)` as a violation under `type[T]`. [`TypeIs`](https://peps.python.org/pep-0742/) (PEP 742, Final) could narrow a feature-bearing type.

## 6. Versioning and experimental marking

**Short answer.** Enforcement strength varies widely, and only compiler opt-ins or runtime refusals hold.

| Mechanism | What happens without opt-in or on mismatch | Source |
| --- | --- | --- |
| Kotlin `@RequiresOptIn` | Compile error by default (`Level.ERROR`), or a warning if the author chooses | [opt-in](https://kotlinlang.org/docs/opt-in-requirements.html) |
| Kotlin `@SubclassOptInRequired` | Same, for subclassing only (since 2.1) | [API](https://kotlinlang.org/api/core/kotlin-stdlib/kotlin/-subclass-opt-in-required/) |
| Rust `#[unstable]` | Unusable without `#![feature]` on the crate, on nightly too | [rustc dev guide](https://rustc-dev-guide.rust-lang.org/stability.html) |
| Terraform protocol | Major encoded in the package name (`tfplugin6`); negotiated at handshake; minors are feature-detected | [protocol README](https://github.com/hashicorp/terraform/blob/main/docs/plugin-protocol/README.md), [docs](https://developer.hashicorp.com/terraform/plugin/terraform-plugin-protocol) |
| ADBC | Init receives a version; a client may abort or retry with an older one | [adbc.h](https://github.com/apache/arrow-adbc/blob/main/c/include/arrow-adbc/adbc.h), [versioning](https://arrow.apache.org/adbc/current/format/versioning.html) |
| SLF4J | `versionSanityCheck` warns; it does not refuse | [LoggerFactory](https://github.com/qos-ch/slf4j/blob/master/slf4j-api/src/main/java/org/slf4j/LoggerFactory.java) |
| pytest | `PytestExperimentalApiWarning`; private constructors warn through [`check_ispytest`](https://github.com/pytest-dev/pytest/blob/main/src/_pytest/deprecated.py) | [warning types](https://github.com/pytest-dev/pytest/blob/main/src/_pytest/warning_types.py) |
| CPython unstable C API | `PyUnstable_` name prefix; may change between minors without deprecation warnings | [PEP 689](https://peps.python.org/pep-0689/) |
| CSI | `alpha_*` proto options. Removal does happen: the spec records that the alpha `VolumeCondition` field was removed | [spec](https://github.com/container-storage-interface/spec/blob/master/spec.md) |
| PEP 411 | Documentation notice only | [PEP 411](https://peps.python.org/pep-0411/) |

- **PEP 411 was not withdrawn.** Its status is **Superseded**, with no successor named in the header. Its note gives two reasons: the status was rarely used, and it "has also not helped prevent people" from relying heavily on provisional modules, so changes still caused breakage. The earlier [PEP 408](https://peps.python.org/pep-0408/) (`__preview__` namespace) was rejected in favour of documentation marking.
- **Do not put stability in the import path.** OpenTelemetry forbids stability terms in directory or import names, and forbids breaking users when a signal becomes stable. New in-development methods should require opt-in ([versioning](https://github.com/open-telemetry/opentelemetry-specification/blob/main/specification/versioning-and-stability.md)).
- **API must stabilize first.** OpenTelemetry requires the API to stabilize before other components.
- **Deprecation tooling.** [PEP 702](https://peps.python.org/pep-0702/) gives `@warnings.deprecated` both a runtime warning and type-checker diagnostics. It has no experimental equivalent.

## 7. Preventing direct imports of a concrete driver

**Short answer.** Where the language has visibility, some ecosystems use it. In Python nothing prevents the import, and only tools can flag it. For Sandloom it is worth doing because the tool already exists.

- **Language-level.**
  - Go: code under `internal` can be imported only from the tree rooted at the parent of `internal` ([cmd/go](https://github.com/golang/go/blob/master/src/cmd/go/alldocs.go)).
  - Java: the docs strongly recommend that a module not export the package holding its service provider ([ServiceLoader](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/ServiceLoader.html)), so a concrete driver can be discoverable and unimportable. This is the closest match to the goal, and it has an escape hatch in [`--add-exports`](https://openjdk.org/jeps/261).
  - Rust: `pub(crate)` ([reference](https://doc.rust-lang.org/reference/visibility-and-privacy.html)) and sealed traits.
- **Prose only.** OpenTelemetry's rule is addressed to authors, and I found no enforcement in the documents I read. Django treats a [leading underscore](https://docs.djangoproject.com/en/5.2/misc/api-stability/) as internal.
- **Go does not prevent it,** and provides [`Conn.Raw`](https://github.com/golang/go/blob/master/src/database/sql/sql.go) as an escape hatch to the driver connection.
- **Python tools.** The typing spec makes underscored names and non-re-exported imports private in `py.typed` packages ([spec](https://typing.python.org/en/latest/spec/distributing.html#library-interface-public-and-private-symbols)). pyright's [`reportPrivateImportUsage`](https://github.com/microsoft/pyright/blob/main/docs/configuration.md) defaults to `error`. Import Linter's [forbidden contract](https://import-linter.readthedocs.io/en/stable/contract_types/forbidden/) covers indirect imports and external packages.

## Ecosystem comparison

| Ecosystem | API / SPI split | Kit | Selection | Duplicate name | Load failure | Capabilities | Version check |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Go `database/sql` | Separate packages | None for `database/sql`; `fstest`, `iotest` for other interfaces | Name in `sql.Open` | Panic | Compile-time link | Optional interfaces | None at runtime |
| SQLAlchemy | Interface + default base | Importable suite + `Requirements` | URL name, lazy | First hit | Raised when selected | Class attributes + `initialize` | No check found |
| Django | Backend base classes | None documented for third parties | Import path in settings | Not applicable | `ImproperlyConfigured` | `DatabaseFeatures` | [Release notes](https://docs.djangoproject.com/en/5.2/releases/5.2/#database-backend-api) list backend changes per version |
| OpenTelemetry | API vs SDK packages | Matrix only | Language-specific | Language-specific | Language-specific | Matrix | Spec rules; opt-in SHOULD |
| SLF4J | `org.slf4j` / `.spi` | None found | ServiceLoader, first, or property | Warn, use first | NOP with warning | None | Warning |
| JDBC | Same interfaces | TCK referenced; not verified | URL, first accepting driver | Tolerated | Swallowed | `DatabaseMetaData`, exception | `jdbcCompliant` self-report |
| pluggy / pytest | Hook specs vs impls | `pytester` | All autoloaded; `-p` to choose | `ValueError` or skip | Raised | Not applicable | `required_plugins` |
| Terraform | Protocol / framework / core | Own acceptance tests | Named provider | Not checked here | Not checked here | Interfaces + `ServerCapabilities` | Handshake negotiation |
| CSI | gRPC services | `csi-sanity`, capability-gated | Endpoint | Not applicable | Not applicable | Capability RPCs | Alpha options |
| fsspec | `AbstractFileSystem` base | `fsspec.tests.abstract` | Protocol name | `ValueError` unless clobber | At use, with `errtxt` | None | None found |

## What fits Sandloom

**All of this section is judgement**, except the "Current state" findings, which come from reading the code at the baseline and running a small offline probe.

### Current state

| Finding | Evidence |
| --- | --- |
| A plugin can silently replace `sqlalchemy` or any built-in. A second `register_driver` of a name succeeds and logs at debug; in the probe it also replaced the features | `registry.py` `register_driver`; the guide says `database` is not to be replaced |
| A driver for a capability declared by another plugin depends on entry-point order. Strict mode raises `UnknownCapabilityError` when the spec is not there yet, and the loader logs and skips it | `registry.py` `load_entrypoint_drivers`, `build_infra_registry`; order is arbitrary (§3) |
| Features are declared twice with no cross-check. In the probe, `register_driver(features={"email"})` with a default `features()` passed `validate_requirement`, then failed `resolve` with `FeatureUnsupportedError` | `registry.py`; the guide's "declare them in `register_driver` too" |
| Some "features" are not optional: `get`, `set`, `add`, `delete`, `incr` are in `CACHE_CAPABILITY.features` and are abstract methods | `capabilities.py`, `contracts.py` |
| `capability_version` is optional, so a driver can skip the only version check | `register_driver` signature |
| `notifier()`, `objectstore()`, `taskqueue()` and `capability()` return `Any`; `capability(contract=)` is untyped. `cache()` is typed | `infra/access.py` |
| The concept doc shows `Annotated[GraphStoreProvider, Capability(...)]`, but no provider contract is exported from `sandloom.sdk`, so a module cannot name it within the import rule | grep of `src/sandloom/sdk`; `capabilities-and-infrastructure.md` |
| Callers receive the implementer's type, with `aopen`, `aclose`, `health` and `raw_client` visible; C003 catches only the last | `contracts.py`, error table in the concept doc |
| `sandloom.driver` is not in `public_api_baseline.json`, so its changes pass unreviewed by T008 | checked the baseline |
| "A contract joins only with a kit" is prose; I found no test for it | `driver/__init__.py`, `versioning.md` |
| C004 uses a fixed list (`motor`, `pymongo`, `neo4j`, `temporalio`, `kafka`, `boto3`); `redis`, `celery` and third-party driver packages are not on it | `archcheck/analyzers.py` |
| Already good: the separate `sandloom.driver` package, its name without "experimental", the module and kernel bans (C004, `.importlinter`), build-free preflight, errors with `fix=`, and the kit run against the shipped log driver | ADR-0010, `registry.py`, `test_driver_api.py` |

### Recommendations

1. **Discover drivers by name, lazily, with no `register` callable.**
   - Entry point: `[project.entry-points."sandloom.drivers"] "notification.file" = "sandloom_notify_file:FileNotifier"`. A plugin's own capability gets a second group, `sandloom.capabilities`: `vector_index = "pkg:VECTOR_INDEX"`.
   - The kernel keeps its built-in table and consults it first. It imports only what `INFRA` selects.
   - This follows SQLAlchemy. It removes ordering and multi-phase questions, keeps unselected plugins from running code at startup, and lets diagnostics list drivers without importing them, which suits the cold-reader rule in `design/context-and-graph`.
   - `InfraRegistry` then leaves the driver API. The `s3`, `gcs` and `yc` placeholders can go, since "no such driver" becomes the ordinary error naming the package to install.
2. **Make conflicts and failures hard errors, with new error codes.**
   - Two distributions publishing a name selected by `INFRA`, or a plugin claiming a built-in name: refuse at startup and name both distributions. There is no override, because a different behaviour is a different driver name selected in `INFRA`.
   - A selected driver that fails to import: fail at startup with the cause. Unselected plugins never load, so the log-and-continue branch disappears. This matches pytest's reasoning and `DriverManager`'s default.
3. **Declare features once, on the driver class.**
   - `features: ClassVar[frozenset[str]]`, with an optional classmethod `features_for(options)` replacing `instance_features`. Remove the overridable `features()`, and let the registry read the class before building.
   - Reduce each spec's vocabulary to real options: remove abstract-method names from `cache`, `objectstore` and `taskqueue`.
4. **Require the contract version.** Make it a required class attribute and refuse a mismatch at load, as Terraform does at handshake rather than as SLF4J does with a warning. Use the existing vocabulary (`NOTIFICATION_V1`, as in `billing.credits.v1`).
5. **Detect contract changes the way contracts already do.** Snapshot the SPI of each contract in `sandloom.driver`, and fail when a signature changes without a version bump, as T004 says: "a breaking change is a new contract version (.v2), not an edit of .v1". Add `sandloom.driver` to the T008 baseline. "May change in any release" then becomes "an incompatible driver is refused with a fix". Keep the guide's pin on a `sandloom` minor range.
6. **Build each kit as a subclassable pytest class in `sandloom.driver.testing`.**
   - One test per obligation, so failures are named and an agent can act on each. The current function stops at the first failure.
   - Gate on the driver's own class declaration, as Django and CSI do. Do not add a second `Requirements` file; §2 shows how that drifts.
   - Assert that the declaration matches behaviour, and report skipped features.
   - Add meta-tests: every exported contract has a kit; every feature in its spec has a clause; every built-in driver passes; deliberately broken drivers fail (the repo already keeps `tests/architecture/_fixtures`).
7. **Give callers a narrow Protocol and implementers an ABC.**
   - `sandloom.sdk.infra` exports `Notifier` (a Protocol with `send`), and accessors return it instead of `Any`. `sandloom.driver.NotifierProvider` stays an ABC that explicitly subclasses it and adds lifecycle.
   - Modules then cannot type-check a call to `aclose()` or `raw_client()`. The ABC keeps `isinstance` meaningful, which a runtime-checkable Protocol would not.
   - Only for contracts with public drivers, so concept count grows by one type per public capability.
8. **Use `CapabilityRef[P]`, mirroring `ContractRef[P]`.**
   - `NOTIFICATION_V1: CapabilityRef[Notifier] = CapabilityRef("notification.v1")` and `capability(REF, name="default") -> P`, with a `Literal[True]` overload for `optional` as `Contract` already has. Omitting the class argument avoids the `type[T]` trap, and the registry already holds the contract for `isinstance`.
   - Fix the concept doc's `GraphStoreProvider` example at the same time.
9. **Make experimental use by a module an explicit opt-in.** Extend the `# sandloom: advanced <reason>` marker that T009 checks in generated code and examples: `sandloom check` requires `# sandloom: experimental <reason>` where a module uses a ref of an experimental capability. No path rename at graduation, per OpenTelemetry. This is the nearest Python gets to Kotlin's `RequiresOptIn`.
10. **Extend C004 from installed metadata.** Treat the module path in each `sandloom.drivers` entry point as forbidden to business modules, in addition to the fixed SDK list and with the same dynamic-import handling. Add `redis` and `celery` to the fixed list. A plugin keeping its `CapabilityRef` and Protocol outside the driver's module tree remains importable, which gives the OpenTelemetry API and SDK split without a rule exception.

Suggested order: 3, 8 and 7 change what agents see first; 1 and 2 replace the loader; 4, 5, 6, 9 and 10 add enforcement.

### Rejected options

| Option | Why not |
| --- | --- |
| Eager loading with sorted order, plus a capabilities-then-drivers phase | It works, and pluggy's pending-then-`check_pending` shape would be the model. But it still runs every installed plugin at startup and needs conflict rules that lazy lookup avoids |
| Keeping log-and-continue for robustness | The operator gets "no registered driver" for a problem that already has a precise cause. A selected driver that cannot load cannot serve anyway |
| An `override=True` flag (Sphinx, fsspec) | No present need. It gives a third party a way to replace `database`, which the guide says has no public contract. Revisit if a real case appears |
| Pass-through wrappers over providers, like Go's `sql.DB` | Go's wrapper holds pooling and conversion. A wrapper here would add a layer on the cache hot path, with only narrowing as a benefit, and a Protocol gets that for free. Revisit if cross-cutting behaviour moves out of drivers |
| `capability(GraphStoreProvider)` with `type[T]` | Fails mypy for every abstract contract (tested) |
| `TypeForm` | More machinery than a typed key. I did not check its support in mypy or pyright |
| Feature-specific Protocols with `TypeIs` | Adds a concept before any feature needs it. The base contract already gates `compare_and_set` with `NotImplementedError`. Revisit per feature |
| `sandloom.experimental.*` namespace | OpenTelemetry forbids this shape, PEP 408 was rejected, and it forces a rename at graduation |
| Documentation-only experimental status | PEP 411 is the record of how that went. It is also the current state of `sandloom.driver` |
| Runtime `FutureWarning` for experimental use, as in pytest | Runtime warnings are easy to filter and arrive late. A failing static check has a clearer message and fix |
| Accepted-risk entry for experimental capabilities in production | Plausible use of ADR-0006, but the module opt-in already records intent. Add it only if operators need a separate say |
| Import hooks or sealing subclasses at runtime | Neither Python nor the sources offer a reliable form. The check is enough, given the threat is mistakes rather than an attacker |

## What I could not verify

- **JDBC TCK.** The primary evidence is `jdbcCompliant`'s text and a [general JCP description of TCKs](https://jcp.org/en/resources/guide-tck). I found no first-party account of what the JDBC tests check or how they are distributed. A search returned only mailing-list comments, which I did not rely on.
- **Entry-point order within a pytest phase.** The docs order the phases only. Order inside one is my reading of pluggy and `importlib.metadata` source, not a documented behaviour.
- **`importlib.metadata` ordering.** "No ordering statement" means targeted searches of the page found none, and the [entry points spec](https://packaging.python.org/en/latest/specifications/entry-points/) has none in its data-model text.
- **CSI.** I found no versioning policy in the [README](https://github.com/container-storage-interface/spec/blob/master/README.md) or CONTRIBUTING by search, and did not establish whether any tool enforces the `alpha_*` options.
- **Terraform.** Its plugin README says the startup and handshake protocol is undocumented. I relied on that README and on [go-plugin](https://github.com/hashicorp/go-plugin/blob/main/client.go) for the existence of negotiation, and did not trace it. I did not establish how duplicates or load failures behave.
- **OpenTelemetry.** I read the specification, not each language SIG's implementation, so "language-specific" cells are unexamined. Absence of enforcement means absence from the documents read.
- **ADBC.** I read the header, versioning page and the `DriverQuirks` class in `adbc_validation.h`, but did not run or trace its suite, so it is not in the comparison table.
- **mypy behaviour** with `type[P] | None` is an observation on mypy 2.4.1, not documented, and may change. Type-checker results were from a scratch file. The repo's own mypy configuration was not used.
- **go.dev** pages did not yield readable text with my extraction, so Go's `internal` rule is cited from `alldocs.go`, which is the source of the same help text.
- **Sandloom.** Findings come from reading and from an offline probe of `InfraRegistry` with a throwaway script outside the repo. I did not run `sandloom test`. "No test for it" means searches found none, which is not proof of absence.
