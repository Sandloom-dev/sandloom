# Changelog — sandloom-admin

All notable changes to the `sandloom-admin` distribution are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Per-package entries are aggregated in the repository-level [docs/changelog.md](../../docs/changelog.md).

## [Unreleased]

## [0.7.0a6] — 2026-11-01

### Added

- The admin console as a package: a prebuilt web UI served by the application at
  `/admin/`, off in production unless `ADMIN_CONSOLE_ENABLED=true`.
