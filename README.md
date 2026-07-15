# Development Project Monitor

A Python monitor for new Toronto development applications and relevant construction/development news from:

- City of Toronto Open Data `development-applications`
- RENX
- RENXHomes
- SustainableBiz Canada
- FoodNX
- TechNX
- PeopleNX

The monitor is designed to run once per invocation. Put it on cron, GitHub Actions, or a container scheduler. It stores previously seen items in SQLite and only notifies on new matches.

## What it sends

### Toronto Open Data development applications

Toronto is now read from the current CKAN/Open Data `development-applications` package. The monitor prefers the daily `Development Applications.csv` resource and uses the row fields directly.

For each newly detected application, the notification includes:

- file/application number
- address/title
- application type, status, submitted date, ward, and community meeting fields when available
- development description/proposal
- current public `APPLICATION_URL` link when the CSV provides one
- contact fields when available

The monitor no longer scrapes or requests retired `app.toronto.ca/AIC/index.do` or `secure.toronto.ca/AIC/index.do` links. Legacy AIC links are suppressed instead of retried. Current Toronto links are expected to use this format:

```text
https://www.toronto.ca/city-government/planning-development/application-details/?id=<id>&pid=<pid>&title=<title>
```

### Ottawa DevApps status

Ottawa is disabled by default in this package. The current public DevApps search/detail pages are JavaScript-rendered, so a plain HTTP request receives the JavaScript shell rather than the application data. The previously configured `https://devapps-restapi.ottawa.ca/devapps/ExportData` endpoint is also not documented/verified here as a stable public API and should not be treated as production-ready until it is confirmed.

The Ottawa monitor code remains in place for experimentation. If enabled, it now fails loudly when the endpoint returns a JavaScript shell or non-parseable output instead of silently producing empty results.

### News sites

For relevant new posts about site development or new construction, the notification includes:

- source, title and URL
- location
- footprint / size metrics
- building type
- development timelines
- influencing parties
- evidence keywords and a short snippet

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yml config.yml
```

Edit `config.yml` or set environment variables for notifications.

## Run once

```bash
python -m dev_project_monitor.monitor --config config.yml
```

Use `--dry-run` to print matches without sending notifications:

```bash
python -m dev_project_monitor.monitor --config config.yml --dry-run
```

## Schedule with cron

Run every morning at 7:30 a.m. Toronto time:

```cron
TZ=America/Toronto
30 7 * * * cd /opt/dev_project_monitor && .venv/bin/python -m dev_project_monitor.monitor --config config.yml >> monitor.log 2>&1
```

## Schedule with Docker

```bash
docker build -t dev-project-monitor .
docker run --rm \
  -v "$PWD/data:/app/data" \
  -e SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." \
  dev-project-monitor python -m dev_project_monitor.monitor --config config.yml
```

## First run behaviour

By default the first run bootstraps state and does **not** notify every historical item. Set `notify_on_first_run: true` in `config.yml` if you want a full initial digest.

## Notes

Toronto monitoring should stay on CKAN/Open Data. Do not reintroduce AIC page scraping unless the City publishes a new documented machine-readable detail API.
