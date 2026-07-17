# Validation report

## Completed checks

- `dev_project_monitor/monitor.py` passes Python compilation.
- The package imports and its CLI help renders successfully.
- Both YAML configuration files parse successfully.
- Eleven Toronto reconciliation regression tests pass.
- Legacy AIC folder URLs and Application Details pages are rejected as direct documents.
- Canonical Toronto state keys supersede poisoned legacy AIC keys.
- A real application scope wins over stale maintenance text in another frame.
- The first strong ready poll is accepted without a contradictory second probe.
- The exact GitHub mount regression is covered: when the first probe contains
  `supportingTextMatches == 1`, the click occurs before any further scroll.
- GitHub workflow and configuration use headed bundled Chromium under Xvfb.
- The SQLite state cache and v3 browser-profile cache are independent.

## Browser identity launch validation

A local Xvfb launch using the packaged `_launch_toronto_context()` completed
successfully and returned:

```text
mode: bundled-chromium-cdp
user agent: Mozilla/5.0 ... Chrome/144.0.0.0 Safari/537.36
navigator.webdriver: false
```

The package pins Playwright 1.57.0 in `requirements.txt`, so GitHub will use that
version's bundled Chromium build; the exact Chrome version can differ from the
local validation environment. The important validated properties are the CDP
attachment mode, non-HeadlessChrome identity, and `navigator.webdriver=false`.

## Live Toronto limitation in this workspace

This execution environment cannot complete a live Toronto application request,
so the next GitHub Actions `diagnose_url` run remains the authoritative live
validation. The expected revision marker is:

```text
Toronto enrichment revision: 2026-07-17-supporting-click-latch-v3
```

## Workflow synchronization v3.1

- Confirmed `.github/workflows/dev-project-monitor.yml` invokes the monitor through `xvfb-run -a`.
- Added log marker `Toronto workflow revision: 2026-07-17-xvfb-sync-v3.1`.
- Full reconciliation suite: 11 passed.
