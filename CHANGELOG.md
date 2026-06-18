# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

GitHub Release notes are auto-generated from commits on each `vX.Y.Z` tag
(see `.github/workflows/release.yml`); this file is the curated, human-facing
summary.

## [Unreleased]

## [0.12.0] - 2026-06-18

### Added

- Watch panels overhaul (aesthetics + animation + usability), terminal look kept:
  per-panel time progress bars (elapsed / timeout); the swarm dashboard also gains an
  overall done/total bar, per-row time bars, and keyboard navigation (↑/↓ select, ↵
  open); the worker detail window gains the Markdown + typewriter rendering and a copy
  button from the single-worker viewer; a "jump to latest" follow affordance; and
  status-glow / completion-pop animations.

### Changed

- Watch windows are now **reused** across repeated runs instead of stacking a new
  browser window each time watch mode is used — the bridge detects an already-open
  viewer (via its `/events` polling) and lets it pick up the new run (the page resets
  itself; the swarm dashboard rebuilds for the new fan-out). Set
  `AGY_WATCH_ALWAYS_NEW=1` to force a fresh window per run.

## [0.11.0] - 2026-06-18

### Changed

- **BREAKING:** folded the live "watch" view into the single-prompt tools as a
  `watch` flag instead of separate tools. `antigravity_ask`, `antigravity_continue`
  and `antigravity_image` now take **`watch=true`** to open the Antigravity Intern
  browser window — matching `antigravity_swarm`'s existing `watch` flag. This also
  means **`antigravity_continue` gains watch mode** (it had none before). Tool count
  drops from eight to six.
- Swarm dashboard now shows the **full prompt**: dashboard rows wrap to 3 lines
  (were single-line, ellipsis-clipped), and each worker's detail window shows the
  complete, untruncated prompt in an **expandable** PROMPT pane (click to expand /
  collapse). The truncated row caption is unchanged.

### Removed

- **BREAKING:** `antigravity_ask_watch` and `antigravity_image_watch` — superseded
  by `watch=true` on `antigravity_ask` / `antigravity_image`.

## [0.10.4] - 2026-06-18

### Changed

- Docs: refreshed the `bridge version` example in the README so the illustrative
  versions don't imply a stale release is current.

## [0.10.3] - 2026-06-18

### Changed

- Docs: document the in-chat update notice in the README — both surfaces (the
  `bridge version` row in `antigravity_status`, visible in the client's chat,
  and the startup stderr warning in host logs). Refreshes the PyPI
  long-description.

## [0.10.2] - 2026-06-18

### Added

- `antigravity_status` now reports the bridge's own version and whether a newer
  release is available (e.g. `v0.10.1 -> v0.10.2 available; upgrade: uvx
  antigravity-intern@latest`). This surfaces the update notice **in the MCP
  client's chat** — the startup stderr warning only reaches the host's logs.
  Best-effort GitHub check; honors `AGY_BRIDGE_NO_UPDATE_CHECK` and never flips
  the overall status to PROBLEMS FOUND (an available update is informational).

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

[Unreleased]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.4...v0.11.0
[0.10.4]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.3...v0.10.4
[0.10.3]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.2...v0.10.3
[0.10.2]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/SinanTufekci/antigravity-intern/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/SinanTufekci/antigravity-intern/releases/tag/v0.8.0
