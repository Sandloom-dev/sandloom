# Changelog — sandloom-identity

All notable changes to the `sandloom-identity` distribution are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Per-package entries are aggregated in the repository-level [docs/changelog.md](../../docs/changelog.md).

## [Unreleased]

## [0.7.0a6] — 2026-11-01

### Added

- `identity.security.v1` gains `find_user_id` and `appoint_platform_admin`, behind
  `sandloom security grant-admin`.

## [0.7.0a5] — 2026-10-31

- Align the framework dependency floor with `sandloom>=0.7.0a5,<0.8`.

- Fill `Notification.body_fields` on every email, so a notification driver can render
  its own templates. Requires `sandloom>=0.7.0a5,<0.8`.

## [0.7.0a4] — 2026-10-31

- Align the framework dependency floor and package documentation with
  `sandloom>=0.7.0a4,<0.8`.

## [0.7.0a3]

- Depend on `sandloom>=0.7.0a3,<0.8`.

### Security

- Require `authlib>=1.9.1,<2` so fresh dependency resolutions cannot select
  versions affected by the reviewed JOSE/OAuth validation advisories.

## [0.7.0a2]

- Refresh the package description and project links on PyPI.
- Depend on the framework alpha `sandloom>=0.7.0a2,<0.8`.

## [0.7.0a1] — 2026-10-28

Initial public alpha release; earlier version headings describe internal development.

- Breaking (packaging): an ecosystem module distribution. The import package
  is `sandloom_identity` (formerly `sandloom.identity`); it registers the
  `sandloom.modules` entry point `identity = "sandloom_identity"` and depends on
  `sandloom>=0.7,<0.8` instead of `sandloom-framework` + `sandloom-shared`.
  Its contract DTOs, stubs and event schemas stay in `sandloom.shared`
  (shipped with `sandloom`, SANDLOOM-C012).
  `itsdangerous` and `email-validator` are now its own dependencies.
- Preserve database/security infrastructure errors in authentication instead of
  converting them to invalid tokens or anonymous optional users.

## [0.7.0] — 2026-10-13

Internal development milestone; this version was not published to PyPI.
Distributions now follow the [Versioning & Stability](../../docs/contributing/versioning.md)
policy. The change list since 0.6.0 is in [CHANGELOG.md](../../CHANGELOG.md)
and the current source API upgrade steps are in
[Upgrading to Public API 1.0](../../docs/operate/upgrade-1.0.md).

### Fixed
- Source distributions include the package sources and build independently of the monorepo.

### Added
- Identity module: users, JWT authentication, RBAC, 2FA and SSO, shipped as a
  standalone wheel over `sandloom-framework` + `sandloom-shared`.
