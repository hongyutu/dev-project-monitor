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

The monitor uses Playwright-bundled Chromium with a persistent native browser
profile. It waits for Toronto's JavaScript application service, opens only the
Supporting Documentation accordion, clicks the real Application Form download,
and rejects legacy AIC folder pages as document links.

A failed linked application remains unseen and will be retried. Incomplete
Toronto results are not notified by default. Diagnostics are written under
`data/toronto_debug`.

See `RECONCILIATION_NOTES.md` for the detailed merged behaviour.
