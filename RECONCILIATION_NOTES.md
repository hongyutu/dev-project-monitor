# Toronto monitor reconciliation

This package consolidates the successful Toronto crawler behaviour into the
actual `dev_project_monitor.monitor` execution path.

## Active Toronto behaviour

- Launches the Chromium binary bundled with the installed Playwright version.
- Uses `data/toronto_browser_profile` as a persistent browser profile.
- Pins Python Playwright so the bundled Chromium build does not drift between runs.
- Does not override `navigator.userAgent` or silently switch executables.
- Waits for Toronto's JavaScript application service, not only
  `DOMContentLoaded`.
- Accepts the first strong ready observation immediately; it is not re-polled or made conditional on a second probe.
- Targets **Supporting Documentation** directly; page-wide **Expand All** is not
  used.
- Treats `button.downloadFile[data-id]` as an opaque UI control, never a URL.
- Downloads the real Application Form through the browser for owner/applicant
  extraction.
- Reports other required rows as available on the current Application Details
  page when Toronto does not expose a reusable direct file URL.
- Rejects all legacy `app.toronto.ca/AIC/index.do?folderRsn=...` URLs as
  documents.
- Does not replay arbitrary document APIs when the application widget fails.
- Uses the canonical current Toronto URL for state keys, so legacy AIC seen-state entries cannot block the repaired retry.
- Keeps failed or incomplete linked applications unseen for a later retry.
- Does not send misleading partial notifications by default.

## Manual diagnosis

Run locally:

```bash
python -m playwright install chromium
python -m dev_project_monitor.monitor \
  --config config.yml \
  --diagnose-url "https://www.toronto.ca/city-government/planning-development/application-details/?id=5870111&pid=96304&title=130%20FUNDY%20BAY%20BLVD"
```

The GitHub Actions `workflow_dispatch` form now has a `diagnose_url` input that
runs the same state-free diagnosis. Failed runs upload `data/toronto_debug` as a
workflow artifact.

## July 17 readiness hotfix

GitHub Actions showed the application in this sequence: `loading -> ready -> maintenance`.
The previous classifier allowed a generic maintenance template or stale child frame to
downgrade an already-painted application widget. The reconciled classifier now:

- gives real application evidence precedence over maintenance text in another scope;
- probes open shadow roots and child frames for application-specific markers;
- latches the first strong ready state without requiring a second probe; and
- saves only one debug bundle for a single failure instead of duplicate
  `maintenance_response` and `render_exception` directories.

## July 17 ready-latch v2

The first readiness hotfix was still conditional: it accepted the first `ready`
state only when `_application_widget_probe()` separately returned `ready=True`.
The GitHub log proved that the main classifier could find strong application
evidence while that secondary probe did not. Revision
`2026-07-17-ready-latch-v2` removes the contradictory second check and primes
Supporting Documentation immediately after the first strong ready observation.
