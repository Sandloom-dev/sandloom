### Added

- **Identity prints the API form of the e-mailed steps in dev** (tenant-select-and-dev-verify). With `NOTIFIER_LOG_LINKS=true`, the verification and password reset links are followed on stderr by `[dev only] no frontend? POST /auth/verify-email {"token": "..."}` (and the `/auth/reset-password` form), after the change commits. Nothing is printed without the setting.
