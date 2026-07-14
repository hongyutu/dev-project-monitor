# Development Project Monitor

A Python monitor for new Toronto development applications and relevant construction/development news from:

- City of Toronto Application Information Centre / Open Data `development-applications`
- RENX
- RENXHomes
- SustainableBiz Canada
- FoodNX
- TechNX
- PeopleNX

The monitor is designed to run once per invocation. Put it on cron, GitHub Actions, or a container scheduler. It stores previously seen items in SQLite and only notifies on new matches.

## What it sends

### Toronto AIC
For each newly detected application, the notification includes:

- file/application number
- address/title
- application type, status and submitted date when available
- development description/proposal
- AIC detail URL
- matched document links for:
  - civil / site plan / servicing plan
  - architectural plans
  - structural plans
  - geotechnical report/study
  - hydrogeological report/review

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

The Toronto AIC front end can change. This monitor first uses the official Toronto Open Data CKAN dataset, then falls back to resource downloads. For supporting documents, it follows the AIC detail URL and classifies download links by document title. If the detail page renders documents client-side and links are unavailable in HTML, the notification will say the category was not found rather than guessing.
