# Development Project Monitor

Monitors Toronto development applications, optional Ottawa applications, and
optional development news. State is stored in SQLite so completed applications
are not reported repeatedly.

## Installation

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

For OCR of image-only Application Forms, install Tesseract as well.

## Normal run

```bash
python -m dev_project_monitor.monitor --config config.yml
```

Dry run:

```bash
python -m dev_project_monitor.monitor --config config.yml --dry-run
```

## Diagnose one Toronto page without state

```bash
python -m dev_project_monitor.monitor \
  --config config.yml \
  --diagnose-url "TORONTO_APPLICATION_DETAILS_URL"
```

## Retry an application already marked seen

```bash
python -m dev_project_monitor.monitor \
  --config config.yml \
  --forget-url "TORONTO_APPLICATION_DETAILS_URL"
```

## Toronto browser behaviour

The monitor starts Playwright-bundled Chromium as a normal headed browser under
Xvfb and attaches over CDP using a persistent profile. It waits for Toronto's
JavaScript application service, clicks Supporting Documentation at its first
visible instant, downloads the real Application Form, and rejects legacy AIC
folder pages as document links.

A failed linked application remains unseen and will be retried. Incomplete
Toronto results are not notified by default. Diagnostics are written under
`data/toronto_debug`.

See `RECONCILIATION_NOTES.md` for the detailed merged behaviour.

## Applying reconciled ZIP updates

The `.github` directory is hidden on Linux/macOS and is not matched by shell wildcards such as `*`. When replacing repository contents, explicitly copy `.github/workflows/dev-project-monitor.yml`, or extract the ZIP directly over the repository root. The reconciliation test intentionally fails when the workflow and Python/config files are from different package revisions.
