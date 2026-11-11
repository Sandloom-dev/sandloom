"""kb-research-settings, probe 2: what a per-module settings model costs.

Times (median of N, fresh classes, in one process): the kernel Settings(),
a module BaseSettings carrying the 22 ModulesConfig fields with the kernel's
sources (env, .env, SECRETS_DIR), the same as a plain BaseModel validated
from one shared snapshot of the sources, entry-point enumeration, and the
dotenv read. Also checks: one env name declared by two models is not detected
by pydantic-settings; a secret file is found by alias in SECRETS_DIR.
"""

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

work = Path(tempfile.mkdtemp(prefix="kb-settings-"))
secrets = work / "secrets"
secrets.mkdir()
(secrets / "STRIPE_SECRET_KEY").write_text("sk_file_secret")
(work / ".env").write_text(
    "DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:9/db\nCACHE_URL=redis://127.0.0.1:9/9\n"
    "BROKER_URL=redis://127.0.0.1:9/9\nJWT_SECRET=probe-not-a-secret-0123456789abcdef0123\n"
    + "".join(f"FILLER_{i}=x\n" for i in range(40))
)
os.chdir(work)
os.environ["SECRETS_DIR"] = str(secrets)
os.environ["BILLING_DENY_ON_PAST_DUE"] = "true"

from importlib import metadata  # noqa: E402

from pydantic import BaseModel  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

from sandloom.core.config.modules import ModulesConfig  # noqa: E402
from sandloom.core.config.settings import Settings  # noqa: E402

N = 30


def timed(fn):
    samples = []
    for _ in range(N):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


class ModuleSettings(ModulesConfig, BaseSettings):
    """A module-owned model with the kernel's sources (same config + hook)."""

    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False, secrets_dir=str(secrets),
        populate_by_name=True,
    )


class Plain(ModulesConfig):
    """A plain BaseModel: validated from a dict the kernel already read."""


print("python", sys.version.split()[0])
print(f"kernel Settings() median ms: {timed(Settings):.2f}")
print(f"module BaseSettings (22 fields, own env+.env+secrets read) median ms: {timed(ModuleSettings):.2f}")
snapshot = {k.upper(): v for k, v in os.environ.items()}
print(f"module BaseModel.model_validate(shared snapshot) median ms: "
      f"{timed(lambda: Plain.model_validate(snapshot)):.3f}")
print(f"metadata.entry_points().select('sandloom.modules') median ms: "
      f"{timed(lambda: tuple(metadata.entry_points().select(group='sandloom.modules'))):.2f}")
m = ModuleSettings()
print("secret from SECRETS_DIR file by alias:", m.stripe_secret_key == "sk_file_secret")
print("env overrides default:", m.billing_deny_on_past_due is True)


class A(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    api_key: str = "a"


class B(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")
    api_key: str = "b"


os.environ["API_KEY"] = "shared"
print("two models, one env name API_KEY -> both read it silently:", A().api_key, B().api_key)

# The same two constructions with no .env and no SECRETS_DIR (a container that
# passes everything through the process environment).
empty = Path(tempfile.mkdtemp(prefix="kb-settings-empty-"))
os.chdir(empty)
del os.environ["SECRETS_DIR"]
for key, value in (("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:9/db"),
                   ("CACHE_URL", "redis://127.0.0.1:9/9"), ("BROKER_URL", "redis://127.0.0.1:9/9"),
                   ("JWT_SECRET", "probe-not-a-secret-0123456789abcdef0123")):
    os.environ[key] = value


class ModuleSettingsEnvOnly(ModulesConfig, BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)


print(f"[env only] kernel Settings() median ms: {timed(Settings):.2f}")
print(f"[env only] module BaseSettings median ms: {timed(ModuleSettingsEnvOnly):.2f}")
