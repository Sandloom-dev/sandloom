# Security reports

Use [GitHub private vulnerability reporting](https://github.com/Sandloom-dev/sandloom/security/advisories/new)
when it is enabled on the canonical repository. During private release preparation,
this channel is not yet confirmed; a monitored reporting channel must be enabled
before public release. No email address is advertised until it is monitored.
Do not publish credentials, customer data or a working exploit in a public
issue. A report should include the affected commit/version, deployment mode,
minimal reproduction and expected security boundary.

Supported production infrastructure is PostgreSQL and Redis with the documented
strict profile. Architecture checks admit trusted module code; in-process
Python modules are not a sandbox for malicious code.

When a credential may have been exposed, revoke or rotate it at the issuing
provider. Removing a value from the current tree does not revoke it or remove
its copies from Git history. `tools/checks/check_secrets.py` is a narrow detection
backstop and does not certify the absence of secrets.

Supply-chain controls (CycloneDX SBOM artifact, ruff `S` SAST, `pip-audit`, PR dependency review) are described in
`docs/contributing/supply-chain.md`.
