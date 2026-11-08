# Module settings (D2a: plain pydantic)

A module hands a plain pydantic model to its registration:
`define_module(__name__, ..., settings=CatalogSettings)`. The loader validates
it from the process environment (env > `.env` > `SECRETS_DIR`) **before**
`setup` runs; an invalid value stops every entry point with
`[core.config] Invalid configuration: module catalog: <reason>`.

```python
from pydantic import BaseModel, Field, SecretStr, ValidationInfo, model_validator
from sandloom.sdk.module import RemovedSetting, Risk, SemanticSetting, settings_of

class BillingSettings(BaseModel):
    provider: str = Field(default="fake", alias="BILLING_PROVIDER")
    stripe_key: SecretStr | None = Field(default=None, alias="STRIPE_SECRET_KEY")
    retries: int = Field(default=5, ge=1, le=100, alias="BILLING_RETRIES")
    grant_intentless: bool = Field(default=False, alias="BILLING_GRANT_INTENTLESS")

    semantic = (SemanticSetting("BILLING_GRANT_INTENTLESS", "why it weakens a guarantee",
                                profile={"dev": False, "test": False, "prod": False, "prod_strict": False},
                                strength=(True, False)),)          # weakest -> strongest
    risks = (Risk("fake_psp_in_prod", "what accepting it weakens", "who consults it"),)
    removed = (RemovedSetting("BILLING_OLD_FLAG", "0.8", "hint for the operator"),)

    @model_validator(mode="after")
    def _prod_check(self, info: ValidationInfo) -> "BillingSettings":
        ctx = info.context or {}
        if ctx.get("environment") == "prod" and self.provider == "fake" \
                and not ctx["accepts_risk"]("fake_psp_in_prod"):
            raise ValueError("BILLING_PROVIDER=fake is not allowed with ENVIRONMENT=prod")
        return self
```

- Env names are the field aliases; a new name starts with the module prefix (`CATALOG_`).
- Secrets are `SecretStr` (`.get_secret_value()` to read).
- `semantic`, `risks`, `removed` (also `shared`, `console`, `backup`) are class
  attributes holding the kernel's dataclasses. Fields not listed in
  `semantic` are operations settings.
- A risk's full id is `<module>.<id>`: operators accept it with
  `SANDLOOM_ACCEPTED_RISKS=["billing.fake_psp_in_prod"]`; in the check,
  `ctx["accepts_risk"]("fake_psp_in_prod")` takes the local id.
- Production checks are `model_validator`s reading `info.context["environment"]`
  and `info.context["accepts_risk"]`; raising refuses (the first problem).
- Read: `settings_of(BillingSettings)` inside setup, a request or a task —
  never at import (it raises with no app bound).
- Tests: `from sandloom.sdk.testing import override_settings` —
  `with override_settings(BillingSettings, retries=3): ...` (field names), or
  `patch_settings(monkeypatch, BillingSettings, retries=3)`.
