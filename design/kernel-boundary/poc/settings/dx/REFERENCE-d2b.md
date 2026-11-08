# Module settings (D2b: `ModuleSettings` helpers)

A module declares a `ModuleSettings` subclass and hands it to its
registration: `define_module(__name__, ..., settings=CatalogSettings)`. The
loader validates it from the process environment (env > `.env` >
`SECRETS_DIR`) **before** `setup` runs; an invalid value stops every entry
point with `[core.config] Invalid configuration: module catalog: <reason>`.

```python
from pydantic import SecretStr
from sandloom_d2b import CheckContext, ModuleSettings, RemovedSetting, Risk, Semantic, prod_check, setting

class BillingSettings(ModuleSettings):
    provider: str = setting("fake", env="BILLING_PROVIDER", console=True)
    stripe_key: SecretStr | None = setting(None, env="STRIPE_SECRET_KEY")
    retries: int = setting(5, env="BILLING_RETRIES", ge=1, le=100)
    grant_intentless: bool = setting(False, env="BILLING_GRANT_INTENTLESS",
                                     semantic=Semantic(why="why it weakens a guarantee",
                                                       weaker=True, risk=None))
    risks = (Risk("fake_psp_in_prod", "what accepting it weakens", "who consults it"),)
    removed = (RemovedSetting("BILLING_OLD_FLAG", "0.8", "hint for the operator"),)

    @prod_check
    def _no_fake_psp(self, ctx: CheckContext) -> str | None:
        if self.provider == "fake" and not ctx.accepts_risk("fake_psp_in_prod"):
            return "BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod"
        return None
```

- `setting(default, env=..., semantic=..., shared=..., console=..., backup=..., **constraints)`;
  `ge`/`le`/... are pydantic constraints. A new env name starts with the module prefix (`CATALOG_`).
- Secrets are `SecretStr` (`.get_secret_value()` to read).
- `Semantic(why=..., weaker=True)` marks a semantic setting (bool: `weaker` is
  the weaker value). Fields without it are operations settings.
- A risk's full id is `<module>.<id>`: operators accept it with
  `SANDLOOM_ACCEPTED_RISKS=["billing.fake_psp_in_prod"]`; in a check,
  `ctx.accepts_risk("fake_psp_in_prod")` takes the local id.
- `@prod_check` methods run only with ENVIRONMENT=prod; each returns a problem
  string or `None`; every problem is reported.
- Read: `BillingSettings.current()` inside setup, a request or a task — never
  at import (it raises with no app bound).
- Tests: `from sandloom.sdk.testing import override_settings` —
  `with override_settings(BillingSettings, retries=3): ...` (field names), or
  `patch_settings(monkeypatch, BillingSettings, retries=3)`.
