# Changelog

## [Unreleased]

- Preparation of the first public repository package.
- Documented the separate `legal-analysis-rf-sources` integration as optional
  local layer C.
- Clarified standard user storage, explicit custom-path configuration, and the
  fact that downloaded corpus data is not committed to the repository.

## [0.1.1] - 2026-10-06

- Added bounded retries with exponential backoff and jitter for unstable
  connections to source A.
- Added handling for transient `URLError` and `ConnectionError` failures.

## [0.1.0] - 2026-10-05

- Added the initial `min` and `max` document profiles.
- Added network installation and single-document fetching from source A.
- Added local snapshot installation through an explicit `--source` path.
- Added manifest provenance, revision tracking, and non-destructive updates.
