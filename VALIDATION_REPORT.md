# Validation report

## Completed checks

- `dev_project_monitor/monitor.py` passes Python compilation.
- The package imports and its CLI help renders successfully.
- Both YAML configuration files parse successfully.
- Six reconciliation regression tests pass:
  - legacy AIC folder URLs are rejected as documents;
  - Application Details URLs are not treated as direct files;
  - proven document-row availability is preserved;
  - application-service readiness distinguishes loading, ready, and maintenance;
  - notifications label page-only availability correctly;
  - canonical Toronto state keys supersede poisoned legacy AIC keys.

## Live browser limitation in this workspace

A live Fundy Bay diagnosis was attempted. The local execution environment
blocked navigation to `toronto.ca` with Chromium's
`net::ERR_BLOCKED_BY_ADMINISTRATOR`, so a successful Toronto network run could
not be completed here. The failure path correctly returned `failed`, did not
fabricate document URLs, and generated debug artifacts. GitHub Actions installs
the pinned Playwright Chromium build and includes a manual `diagnose_url` input
for the actual deployment test.
