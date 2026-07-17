# Validation report

## Completed checks

- `dev_project_monitor/monitor.py` passes Python compilation.
- The package imports and its CLI help renders successfully.
- Both YAML configuration files parse successfully.
- Nine reconciliation regression tests pass:
  - legacy AIC folder URLs are rejected as documents;
  - Application Details URLs are not treated as direct files;
  - proven document-row availability is preserved;
  - application-service readiness distinguishes loading, ready, and maintenance;
  - notifications label page-only availability correctly;
  - canonical Toronto state keys supersede poisoned legacy AIC keys;
  - a real application scope wins over stale maintenance text in another frame;
  - the first strong ready poll is latched even when a separate widget probe would not corroborate it.

## Live browser limitation in this workspace

A live Fundy Bay diagnosis was attempted. The local execution environment
blocked navigation to `toronto.ca` with Chromium's
`net::ERR_BLOCKED_BY_ADMINISTRATOR`, so a successful Toronto network run could
not be completed here. The failure path correctly returned `failed`, did not
fabricate document URLs, and generated debug artifacts. GitHub Actions installs
the pinned Playwright Chromium build and includes a manual `diagnose_url` input
for the actual deployment test.

## Ready-latch v2 validation

The regression suite now asserts that a first strong `ready` state is terminal
even when `_application_widget_probe()` reports false and even when any second
poll would have become `maintenance`. It also verifies that Supporting
Documentation is targeted before secondary probing or browser identity logging.
