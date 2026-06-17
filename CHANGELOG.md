# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

GitHub Release notes are auto-generated from commits on each `vX.Y.Z` tag
(see `.github/workflows/release.yml`); this file is the curated, human-facing
summary.

## [Unreleased]

## [0.10.1] - 2026-06-18

### Changed

- Docs: reworked install guidance now that the package is live on PyPI.
  `uvx antigravity-intern` is the recommended install, with **opt-in** updates —
  uvx caches and does not auto-upgrade, so the bridge only runs a release you
  chose to install (it runs unsandboxed code, so this is deliberate). Upgrade
  with `uvx antigravity-intern@latest`; `@latest` in the config opts into
  hands-off auto-updates. Corrected an earlier inaccurate "latest on every
  launch" claim, and swapped the GitHub-release badge for a PyPI version badge.

## [0.10.0] - 2026-06-17

### Added

- **PyPI packaging + `uvx` install.** `antigravity-intern` is now an installable
  package with an `antigravity-intern` console entry point, so it can be launched
  with `uvx antigravity-intern` (isolated) instead of a hardcoded path to
  `server.py`.
- **MCP tool annotations.** All eight tools now carry MCP annotations
  (`readOnlyHint` / `idempotentHint` / `openWorldHint` / `title`) so clients can
  reason about which tools are safe — `antigravity_status` is read-only and
  idempotent; the agy-invoking tools are flagged open-world and non-read-only.
- **Native MCP progress notifications.** `antigravity_ask`, `antigravity_continue`
  and `antigravity_image` now emit MCP progress (a coarse elapsed/timeout bar)
  while agy works, for clients that send a progress token. The browser "watch"
  tools are unchanged.
- **CI** (`.github/workflows/ci.yml`): ruff + offline tests on Windows / macOS /
  Linux across Python 3.10–3.13.
- **Release automation**: tagging `vX.Y.Z` cuts a GitHub Release with generated
  notes (`release.yml`) and publishes to PyPI via Trusted Publishing
  (`publish.yml`).

## [0.9.0] - 2026-06-17

### Added

- **Startup update check.** The server polls the GitHub tags API once at launch
  and logs a one-line warning if a newer release is tagged than the running
  `__version__`. Best-effort: silent when offline/rate-limited, never blocks
  startup. Opt out with `AGY_BRIDGE_NO_UPDATE_CHECK=1`; point at a fork with
  `AGY_BRIDGE_REPO`.

## [0.8.0] - 2026-06-17

### Added

- `antigravity_swarm` and `antigravity_image_swarm`: run several agy workers in
  parallel, each in an isolated state dir, with error isolation.
- Live browser "watch" mode (`antigravity_ask_watch`, `antigravity_image_watch`)
  that streams agy's steps and shows generated images inline.

### Changed

- **BREAKING:** rebranded to "Antigravity Intern"; tools renamed `agy_*` →
  `antigravity_*`.

### Removed

- **BREAKING:** `antigravity_ask_stream` (superseded by watch mode).

[Unreleased]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.1...HEAD
[0.10.1]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/SinanTufekci/antigravity-intern/releases/tag/v0.8.0
