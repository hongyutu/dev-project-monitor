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

## July 17 Supporting Documentation click-latch v3

The GitHub log then showed a second independent timing defect:
`supportingTextMatches` became 1 at mount step 1, but the monitor ignored that
signal, continued scrolling until the application widget disappeared, reset to
the top, and only then attempted the click.

Revision `2026-07-17-supporting-click-latch-v3` now:

- treats the first Supporting Documentation text match as an immediate action signal;
- clicks before another scroll or slow poll can replace the widget;
- verifies table/accordion state in a tight no-scroll window;
- searches any visible element, including nested div/span/header variants, and
  walks to an actionable ancestor or descendant;
- falls back to Playwright's exact text locator, which pierces open shadow roots;
- uses smaller, faster progressive scroll steps only while no section text exists;
- does not reset the page to the top before the targeted click.

GitHub Actions now runs headed Chromium under Xvfb. More importantly, the monitor
starts Playwright's bundled Chromium as a normal browser process and attaches to
it over CDP. This retains the version-matched executable and persistent profile
but avoids Playwright's launch automation flag. The validated identity is a
normal `Chrome/...` user agent with `navigator.webdriver == false`, rather than
`HeadlessChrome/...` with `navigator.webdriver == true`.

The SQLite state cache and browser-profile cache are separated. The browser cache
uses a v3 key, while the existing monitor state continues to restore normally.
Service-worker and render caches are cleared at launch, but cookies and local
storage remain persistent.
