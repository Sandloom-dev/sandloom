# Changelog — sandloom-standard

All notable changes to the `sandloom-standard` distribution are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Per-package entries are aggregated in the repository-level [docs/changelog.md](../../docs/changelog.md).

## [Unreleased]

## [0.7.0a12]

### Added

- The distribution: `sandloom_standard.main` exposes `app` and `core`, the kernel
  factory with the lean root's default module set (every installed
  `sandloom.modules` entry point, then a framework checkout's `LOCAL_MODULES`;
  `sandloom.local_modules.default_modules`), so both roots compose the same
  modules (ADR-0012 decision 3). `sandloom[standard]` depends on it; the
  Sandloom image and the shipped compose profiles boot every process from it
  with `SANDLOOM_COMPOSITION_ROOT=sandloom_standard.main`.
