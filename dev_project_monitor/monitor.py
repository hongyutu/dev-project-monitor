"""Development project monitor.

Run once per invocation. State is stored in SQLite so the next invocation can
notify only on new Toronto development applications and relevant news posts.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import email.utils
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import smtplib
import sqlite3
import sys
import tempfile
import time
import textwrap
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote_plus, urlencode, unquote_plus, urljoin, urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

LOGGER = logging.getLogger("dev_project_monitor")

DEFAULT_CONFIG: dict[str, Any] = {
    "state_db": "data/dev_project_monitor.sqlite3",
    "notify_on_first_run": False,
    "request_timeout_seconds": 30,
    "user_agent": "KellerEnterpriseDevProjectMonitor/1.0",
    "toronto": {
        "enabled": True,
        "ckan_base_url": "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action",
        "package_id": "development-applications",
        "fallback_csv_url": (
            "https://ckan0.cf.opendata.inter.prod-toronto.ca/"
            "dataset/0aa7e480-9b48-4919-98e0-6af7615b7809/"
            "resource/77f8a66a-bd43-40e6-b6c9-12a2b03a5032/"
            "download/development-applications.csv"
        ),
        # Read the full CSV first; cap only after filtering/grouping.
        "max_records": 0,
        "max_candidates": 500,
        # Bound expensive detail-page rendering. Unprocessed unseen items remain
        # unmarked and are picked up on the next invocation.
        "max_detail_pages_per_run": 25,
        "dry_run_max_detail_pages": 5,
        "raw_record_limit": 0,
        "render_with_playwright": True,
        "browser_channel": "chromium",
        "browser_profile_dir": "data/toronto_browser_profile",
        "application_service_timeout_seconds": 90,
        "application_service_retries": 3,
        "application_service_poll_ms": 750,
        "application_service_ready_confirmations": 2,
        "report_partial_on_enrichment_failure": False,
        "save_debug_artifacts_on_failure": True,
        "debug_artifacts_dir": "data/toronto_debug",
        "page_timeout_ms": 60000,
        "ocr_image_pdfs": True,
        "extract_application_form_contacts": True,
        "application_form_download_timeout_ms": 30000,
        "playwright_default_timeout_ms": 2500,
        "supporting_docs_mount_wait_seconds": 9,
        "document_rows_wait_seconds": 6,
        "inspect_child_frames": True,
        "application_form_lower_start_ratio": 0.42,
        "ocr_timeout_seconds": 20,
        "source_staleness_warning_days": 14,
        "lookback_days": 45,
        "application_types": [],
    },
    "ottawa": {
        "enabled": False,
        "export_url": "https://devapps-restapi.ottawa.ca/devapps/ExportData",
        "detail_base_url": "https://devapps.ottawa.ca/en/applications",
        "max_records": 2000,
        "lookback_days": 45,
        "application_types": [
            "Official Plan Amendment",
            "Plan of Subdivision",
            "Plan of Condominium",
            "Site Plan Control",
            "Zoning By-law Amendment",
        ],
    },
    "news": {
        "enabled": True,
        "max_posts_per_site": 50,
        "lookback_days": 21,
        "minimum_keyword_score": 2,
        "sites": [],
    },
    "notifications": {
        "delivery_mode": "digest",
        "simplified_report": False,
        "slack": {"enabled": False, "webhook_url_env": "SLACK_WEBHOOK_URL"},
        "smtp": {
            "enabled": False,
            "host_env": "SMTP_HOST",
            "port_env": "SMTP_PORT",
            "username_env": "SMTP_USER",
            "password_env": "SMTP_PASSWORD",
            "from_env": "SMTP_FROM",
            "to_env": "NOTIFY_EMAIL_TO",
            "use_tls": True,
        },
        "generic_webhook": {"enabled": False, "url_env": "NOTIFY_WEBHOOK_URL"},
    },
}

NEWS_KEYWORDS = {
    "development": 2,
    "develop": 2,
    "construction": 2,
    "construct": 2,
    "breaks ground": 3,
    "groundbreaking": 3,
    "underway": 2,
    "site plan": 3,
    "rezoning": 3,
    "proposal": 2,
    "proposed": 2,
    "new facility": 3,
    "new plant": 3,
    "warehouse": 2,
    "industrial": 2,
    "logistics": 2,
    "distribution centre": 3,
    "distribution center": 3,
    "data centre": 3,
    "data center": 3,
    "mixed-use": 2,
    "condo": 2,
    "rental": 1,
    "residential tower": 3,
    "office tower": 3,
    "manufacturing": 2,
    "expansion": 2,
    "campus": 1,
    "acres": 1,
    "square feet": 1,
    "sq. ft": 1,
    "square metres": 1,
}

NEGATIVE_NEWS_KEYWORDS = {
    "podcast": -2,
    "appointment": -2,
    "appointed": -2,
    "executive shakeup": -2,
    "earnings": -1,
    "market report": -1,
    "opinion": -1,
}

DOCUMENT_PATTERNS: dict[str, list[str]] = {
    "application_form": [
        r"application\s+form",
        r"development\s+application\s+form",
        r"planning\s+application\s+form",
        r"application\s+summary",
    ],
    "civil_site_plan": [
        r"civil",
        r"site\s+plan",
        r"site\s+servic",
        r"servicing\s+plan",
        r"functional\s+servicing",
        r"stormwater",
        r"grading",
        r"utilities?\s+plan",
    ],
    "architectural": [
        r"architectural",
        r"floor\s+plan",
        r"elevations?",
        r"sections?",
        r"building\s+plans?",
        r"renderings?",
    ],
    "structural": [r"structural", r"structure\s+plan", r"shoring"],
    "geotechnical": [r"geotechnical", r"geo[-\s]?tech", r"soil\s+report", r"subsurface"],
    "hydrogeological": [r"hydrogeological", r"groundwater", r"dewatering", r"hydrology"],
}

FIELD_ALIASES: dict[str, list[str]] = {
    "file_number": ["file number", "filenumber", "application number", "application_number", "app number", "planning application number"],
    "address": ["address", "location", "municipal address", "properties", "property address"],
    "description": ["description", "proposal", "application description", "project description", "details"],
    "application_type": ["application type", "type", "app type"],
    "status": ["status", "application status", "milestone status"],
    "submitted_date": ["submitted date", "submission date", "date submitted", "received date", "created date"],
    "last_updated": ["last updated", "modified", "date updated", "updated date"],
    "detail_url": ["url", "link", "aic url", "application url", "details url", "application details url"],
    "id": ["id", "application id", "folder id"],
    "pid": ["pid", "property id", "parcel id"],
    "ward": ["ward", "ward number"],
    "district": ["district", "community council", "city district"],
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None) -> dict[str, Any]:
    config = DEFAULT_CONFIG
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        config = deep_merge(DEFAULT_CONFIG, loaded)
    return config


def normalize_key(text: Any) -> str:
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).strip())


def compact_key(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", normalize_key(text)).lower()


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = date_parser.parse(str(value), fuzzy=True)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def shorten(text: str | None, max_len: int = 750) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", html.unescape(str(text))).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1].rstrip() + "..."


class HttpClient:
    def __init__(self, user_agent: str, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/json,*/*"})

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        response = self.session.get(url, timeout=self.timeout_seconds, **kwargs)
        response.raise_for_status()
        return response

    def post_json(self, url: str, payload: dict[str, Any]) -> requests.Response:
        response = self.session.post(url, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response


class StateStore:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_items (
                source TEXT NOT NULL,
                item_key TEXT NOT NULL,
                first_seen_utc TEXT NOT NULL,
                payload_hash TEXT,
                PRIMARY KEY (source, item_key)
            )
            """
        )
        self.conn.commit()

    def is_empty(self) -> bool:
        cur = self.conn.execute("SELECT COUNT(*) FROM seen_items")
        return int(cur.fetchone()[0]) == 0

    def source_is_empty(self, source: str) -> bool:
        cur = self.conn.execute("SELECT COUNT(*) FROM seen_items WHERE source = ?", (source,))
        return int(cur.fetchone()[0]) == 0

    def has_seen(self, source: str, item_key: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE source = ? AND item_key = ?", (source, item_key)
        )
        return cur.fetchone() is not None

    def mark_seen(self, source: str, item_key: str, payload_hash: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen_items(source, item_key, first_seen_utc, payload_hash)
            VALUES (?, ?, ?, ?)
            """,
            (source, item_key, utcnow().isoformat(), payload_hash),
        )
        self.conn.commit()

    def delete_seen(self, source: str, item_keys: Iterable[str]) -> int:
        keys = [normalize_key(key) for key in item_keys if normalize_key(key)]
        deleted = 0
        for key in keys:
            cur = self.conn.execute(
                "DELETE FROM seen_items WHERE source = ? AND item_key = ?",
                (source, key),
            )
            deleted += int(cur.rowcount or 0)
        self.conn.commit()
        return deleted


@dataclasses.dataclass
class NotificationItem:
    source: str
    item_key: str
    title: str
    url: str | None
    kind: str
    payload: dict[str, Any]


class TorontoOpenDataMonitor:
    """Monitor Toronto Development Applications through the current Open Data CKAN feed."""

    FALLBACK_CSV_URL = (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/"
        "dataset/0aa7e480-9b48-4919-98e0-6af7615b7809/"
        "resource/77f8a66a-bd43-40e6-b6c9-12a2b03a5032/"
        "download/development-applications.csv"
    )
    LEGACY_AIC_PATTERNS = (
        "app.toronto.ca/aic/",
        "secure.toronto.ca/aic/",
        "app.toronto.ca/aic/index.do",
        "secure.toronto.ca/aic/index.do",
    )

    REQUIRED_DOCUMENTS: dict[str, list[str]] = {
        "Application Form": [r"application\s+form"],
        "Architectural Plans": [r"architectural", r"architectural\s+plans?", r"elevations?", r"floor\s+plans?"],
        "Civil and Utilities Plans": [
            r"civil",
            r"civil\s+and\s+utilities",
            r"utilities?\s+plans?",
            r"site\s+servic",
            r"servicing\s+plans?",
            r"grading",
            r"stormwater",
        ],
        "Geotechnical Study": [r"geotechnical", r"geo[-\s]?tech", r"soil\s+(?:report|study)"],
        "Hydrogeological Report": [r"hydrogeological", r"hydrogeology", r"groundwater", r"dewatering"],
    }

    def __init__(self, http: HttpClient, config: dict[str, Any]) -> None:
        self.http = http
        self.config = config
        self.ckan_base = config["ckan_base_url"].rstrip("/")
        self.package_id = config.get("package_id", "development-applications")
        self.fallback_csv_url = config.get("fallback_csv_url") or self.FALLBACK_CSV_URL

    def fetch_new_candidates(self) -> list[dict[str, Any]]:
        records = self._fetch_ckan_records()
        lookback_days = int(self.config.get("lookback_days", 45))
        cutoff = utcnow() - timedelta(days=lookback_days)
        normalized_rows: list[dict[str, Any]] = []
        allowed_types = {compact_key(x) for x in self.config.get("application_types", []) if x}

        for index, record in enumerate(records, start=1):
            item = self._normalize_record(record)
            item["csv_row_id"] = normalize_key(record.get("_id")) or str(index)

            if not item.get("file_number") and not item.get("address"):
                continue

            actual_type = compact_key(item.get("application_type"))
            if allowed_types and not self._type_matches(actual_type, allowed_types):
                continue

            date_value = parse_dt(item.get("submitted_date") or item.get("last_updated"))
            if date_value and date_value < cutoff:
                continue

            normalized_rows.append(item)

        LOGGER.info(
            "Toronto Open Data retained %d row(s) after filtering; grouping into application-level records",
            len(normalized_rows),
        )
        grouped = self._group_rows_by_application(normalized_rows)
        grouped.sort(
            key=lambda x: parse_dt(x.get("submitted_date") or x.get("last_updated"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

        dated = [parse_dt(x.get("submitted_date") or x.get("last_updated")) for x in grouped]
        dated = [value for value in dated if value]
        if dated:
            latest = max(dated)
            stale_days = int(self.config.get("source_staleness_warning_days", 14) or 14)
            if latest < utcnow() - timedelta(days=stale_days):
                LOGGER.warning(
                    "Toronto source may be stale: newest application date is %s (older than %d days). "
                    "Verify the configured Open Data/AIC source before relying on notifications.",
                    latest.date().isoformat(),
                    stale_days,
                )

        max_candidates = int(self.config.get("max_candidates", self.config.get("max_records", 0)) or 0)
        if max_candidates > 0:
            grouped = grouped[:max_candidates]

        LOGGER.info(
            "Toronto Open Data filtered %d CSV metadata row(s) into %d application group(s)",
            len(normalized_rows),
            len(grouped),
        )
        return grouped

    def _fetch_ckan_records(self) -> list[dict[str, Any]]:
        """Fetch records from Toronto CKAN, preferring the daily CSV download.

        Do not cap before filtering: Toronto's CSV is not sorted by submitted date.
        """
        resources: list[dict[str, Any]] = []
        raw_record_limit = int(self.config.get("raw_record_limit", 0) or 0)
        try:
            package = self.http.get(
                f"{self.ckan_base}/package_show",
                params={"id": self.package_id},
            ).json()
            if not package.get("success"):
                raise RuntimeError(f"CKAN package_show failed for {self.package_id}: {package}")
            resources = package.get("result", {}).get("resources", []) or []
        except Exception as exc:
            LOGGER.warning("Could not resolve Toronto CKAN package metadata; will try fallback CSV: %s", exc)

        urls_to_try: list[tuple[str, str, str]] = []
        chosen = self._choose_csv_resource(resources)
        if chosen and chosen.get("url"):
            urls_to_try.append(
                (
                    str(chosen["url"]),
                    str(chosen.get("format", "csv")).lower(),
                    str(chosen.get("name") or chosen.get("id") or "CSV resource"),
                )
            )

        if self.fallback_csv_url and all(url != self.fallback_csv_url for url, _, _ in urls_to_try):
            urls_to_try.append((self.fallback_csv_url, "csv", "fallback Development Applications.csv"))

        for resource in resources:
            fmt = str(resource.get("format", "")).lower()
            url = str(resource.get("url") or "")
            if not url or url in {item[0] for item in urls_to_try}:
                continue
            if fmt in {"csv", "json", "geojson"} or url.lower().endswith((".csv", ".json", ".geojson")):
                urls_to_try.append((url, fmt or "csv", str(resource.get("name") or resource.get("id") or "resource")))

        for url, fmt, label in urls_to_try:
            try:
                records = self._fetch_resource_url(url, fmt)
                if records:
                    if raw_record_limit > 0:
                        records = records[:raw_record_limit]
                    LOGGER.info("Toronto Open Data loaded %d record(s) from %s", len(records), label)
                    return records
            except Exception as exc:
                LOGGER.warning("Could not read Toronto Open Data resource %s: %s", url, exc)

        datastore_limit = int(self.config.get("datastore_max_records", 100000) or 100000)
        for resource in [r for r in resources if r.get("datastore_active")]:
            try:
                return self._fetch_datastore_records(resource["id"], max_records=datastore_limit)
            except Exception as exc:
                LOGGER.warning("Could not read Toronto CKAN datastore resource %s: %s", resource.get("id"), exc)

        raise RuntimeError("No usable Toronto Open Data CSV/JSON/datastore resource found for development-applications")

    def _choose_csv_resource(self, resources: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [
            r
            for r in resources
            if str(r.get("format") or "").upper() == "CSV"
            or str(r.get("url") or "").lower().endswith(".csv")
        ]
        if not candidates:
            return None

        def score(resource: dict[str, Any]) -> tuple[int, int, int, int]:
            name = str(resource.get("name") or resource.get("title") or "").strip().lower()
            url = str(resource.get("url") or "").lower()
            resource_id = str(resource.get("id") or "")
            exact_daily_csv = int(name == "development applications.csv")
            download_url = int("/download/development-applications.csv" in url)
            known_daily_resource = int(resource_id == "77f8a66a-bd43-40e6-b6c9-12a2b03a5032")
            avoid_datastore_dump = int("/datastore/dump/" not in url)
            return (exact_daily_csv, download_url, known_daily_resource, avoid_datastore_dump)

        return sorted(candidates, key=score, reverse=True)[0]

    def _type_matches(self, actual_type: str, allowed_types: set[str]) -> bool:
        if not actual_type:
            return False
        return any(
            actual_type == allowed
            or actual_type.startswith(allowed)
            or allowed in actual_type
            or actual_type in allowed
            for allowed in allowed_types
        )

    def _fetch_datastore_records(self, resource_id: str, max_records: int) -> list[dict[str, Any]]:
        url = f"{self.ckan_base}/datastore_search"
        all_records: list[dict[str, Any]] = []
        offset = 0
        page_size = min(1000, max_records)
        while len(all_records) < max_records:
            data = self.http.get(url, params={"resource_id": resource_id, "limit": page_size, "offset": offset}).json()
            if not data.get("success"):
                raise RuntimeError(f"datastore_search failed: {data}")
            page = data.get("result", {}).get("records", [])
            if not page:
                break
            all_records.extend(page)
            offset += len(page)
            if len(page) < page_size:
                break
        return all_records[:max_records]

    def _fetch_resource_url(self, url: str, fmt: str) -> list[dict[str, Any]]:
        response = self.http.get(url)
        text = response.text.lstrip("\ufeff")
        lower_url = url.lower()
        if fmt == "csv" or lower_url.endswith(".csv") or "text/csv" in response.headers.get("content-type", "").lower():
            return list(csv.DictReader(io.StringIO(text)))
        data = response.json()
        if isinstance(data, dict) and "features" in data:
            return [dict(feature.get("properties") or {}, geometry=feature.get("geometry")) for feature in data["features"]]
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "records" in data:
            return data["records"]
        return []

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        keymap = {compact_key(k): k for k in record.keys()}

        def pick(logical: str) -> Any:
            for alias in FIELD_ALIASES[logical]:
                c = compact_key(alias)
                if c in keymap and record.get(keymap[c]) not in (None, ""):
                    return record[keymap[c]]
            for field_compact, original in keymap.items():
                if any(compact_key(alias) in field_compact for alias in FIELD_ALIASES[logical]):
                    if record.get(original) not in (None, ""):
                        return record[original]
            return None

        def exact_field(*names: str) -> str:
            for name in names:
                key = compact_key(name)
                if key in keymap and record.get(keymap[key]) not in (None, ""):
                    return normalize_key(record[keymap[key]])
            return ""

        street_parts = [
            exact_field("STREET_NUM"),
            exact_field("STREET_NAME"),
            exact_field("STREET_TYPE"),
            exact_field("STREET_DIRECTION"),
        ]
        built_address = normalize_key(" ".join(part for part in street_parts if part))

        address = normalize_key(pick("address")) or built_address
        file_number = normalize_key(pick("file_number")) or exact_field("APPLICATION#", "REFERENCE_FILE#")
        raw_detail_url = normalize_key(pick("detail_url")) or exact_field("APPLICATION_URL")
        if not raw_detail_url:
            app_id = exact_field("id", "ID", "APPLICATION_ID", "APP_ID", "APPLICATIONID")
            pid = exact_field("pid", "PID", "PROPERTY_ID", "PROPERTYID", "PARCEL_ID")
            title = exact_field("title", "TITLE", "APPLICATION_TITLE") or address
            if app_id and pid and title:
                raw_detail_url = (
                    "https://www.toronto.ca/city-government/planning-development/application-details/?"
                    + urlencode({"id": app_id, "pid": pid, "title": title})
                )
        detail_url = self._application_details_url(raw_detail_url)

        return {
            "file_number": file_number,
            "address": address,
            "description": shorten(normalize_key(pick("description")), 1200),
            "application_type": normalize_key(pick("application_type")),
            "status": normalize_key(pick("status")),
            "submitted_date": normalize_key(pick("submitted_date")),
            "last_updated": normalize_key(pick("last_updated")),
            "detail_url": detail_url,
            "raw_application_url": raw_detail_url,
            "link_status": self._link_status(raw_detail_url, detail_url),
            "ward": exact_field("WARD_NAME") or normalize_key(pick("ward")),
            "ward_number": exact_field("WARD_NUMBER"),
            "district": normalize_key(pick("district")),
            "community_meeting_date": exact_field("COMMUNITY_MEETING_DATE"),
            "community_meeting_time": exact_field("COMMUNITY_MEETING_TIME"),
            "community_meeting_location": exact_field("COMMUNITY_MEETING_LOCATION"),
            "contact_name": exact_field("CONTACT_NAME"),
            "contact_phone": exact_field("CONTACT_PHONE"),
            "contact_email": exact_field("CONTACT_EMAIL"),
            "parent_folder_number": exact_field("PARENT_FOLDER_NUMBER"),
            "raw": record,
        }

    def _group_rows_by_application(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(self._application_group_key(row), []).append(row)

        applications: list[dict[str, Any]] = []
        total_groups = len(groups)
        for index, (group_key, group_rows) in enumerate(groups.items(), start=1):
            representative = self._choose_representative_row(group_rows)
            addresses = self._unique_sorted(row.get("address") for row in group_rows)
            csv_row_ids = self._unique_sorted(row.get("csv_row_id") for row in group_rows)
            file_numbers = self._unique_sorted(row.get("file_number") for row in group_rows)

            if index == 1 or index == total_groups or index % 5 == 0:
                LOGGER.info(
                    "Toronto grouping progress: %d/%d application group(s) processed (%s; %d metadata row(s))",
                    index,
                    total_groups,
                    representative.get("address") or representative.get("file_number") or group_key,
                    len(group_rows),
                )

            app = dict(representative)
            app.update(
                {
                    "group_key": group_key,
                    "addresses": addresses,
                    "address": addresses[0] if addresses else representative.get("address"),
                    "metadata_row_count": len(group_rows),
                    "csv_row_ids": csv_row_ids,
                    "file_numbers": file_numbers,
                }
            )
            applications.append(app)

        return applications

    def _application_group_key(self, row: dict[str, Any]) -> str:
        details_url = self._application_details_url(row.get("raw_application_url"))
        if details_url:
            return "url:" + normalized_key_part(details_url)

        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        folder_rsn = normalized_key_part(first_raw_value(raw, "FOLDERRSN", "FOLDER_RSN"))
        if folder_rsn:
            return "folder:" + folder_rsn

        file_number = normalized_key_part(row.get("file_number"))
        if file_number:
            return "file:" + file_number

        return "row:" + stable_hash(raw)

    def _choose_representative_row(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        def score(row: dict[str, Any]) -> tuple[int, int, datetime]:
            has_link = int(bool(self._application_details_url(row.get("raw_application_url"))))
            has_description = int(bool(row.get("description")))
            date_value = parse_dt(row.get("submitted_date") or row.get("last_updated")) or datetime.min.replace(tzinfo=timezone.utc)
            return (has_link, has_description, date_value)

        return sorted(rows, key=score, reverse=True)[0]

    def _unique_sorted(self, values: Iterable[Any]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            text = normalize_key(value)
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return sorted(out, key=lambda value: compact_key(value))

    def _link_status(self, raw_url: str | None, detail_url: str | None) -> str:
        if not normalize_key(raw_url):
            return "void"
        if detail_url:
            return "current"
        return "expired"

    def _is_legacy_toronto_url(self, url: str | None) -> bool:
        lowered = normalize_key(url).lower()
        return any(pattern in lowered for pattern in self.LEGACY_AIC_PATTERNS)

    def _application_details_url(self, url: Any) -> str | None:
        """Normalize a Toronto AIC/application URL.

        Toronto's current AIC links use id, pid, and title. Some CSV rows contain
        a title value with raw ampersands, for example ``title=2 & 10 ...``.
        Browsers treat those ampersands as query separators unless we rebuild the
        URL, so canonicalize the query before Playwright opens the page.
        """
        url = html.unescape(normalize_key(url))
        if not url:
            return None
        if url.startswith("/"):
            url = urljoin("https://www.toronto.ca/", url)

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None

        host = parsed.netloc.lower()
        params = parse_qs(parsed.query, keep_blank_values=True)

        def first_param(*names: str) -> str:
            for name in names:
                values = params.get(name) or params.get(name.lower()) or params.get(name.upper())
                if values and normalize_key(values[0]):
                    return normalize_key(values[0])
            return ""

        if self._is_legacy_toronto_url(url):
            folder_rsn = first_param("folderRsn", "folderrsn")
            if not folder_rsn:
                return None
            return (
                "https://www.toronto.ca/city-government/planning-development/"
                "application-details/?"
                + urlencode({"folderRsn": folder_rsn})
            )

        if "toronto.ca" not in host:
            return None
        if "/application-details/" not in parsed.path:
            return None

        folder_rsn = first_param("folderRsn", "folderrsn")
        if folder_rsn:
            return (
                "https://www.toronto.ca/city-government/planning-development/"
                "application-details/?"
                + urlencode({"folderRsn": folder_rsn})
            )

        app_id = first_param("id")
        pid = first_param("pid")
        title = ""
        title_match = re.search(r"(?:\?|&)title=([^#]*)", url, flags=re.I)
        if title_match:
            # Capture to the end so raw ampersands inside title are preserved,
            # then urlencode below turns them into %26.
            title = normalize_key(unquote_plus(title_match.group(1)))
        title = title or first_param("title")

        if not (app_id and pid and title):
            return None

        return (
            "https://www.toronto.ca/city-government/planning-development/"
            "application-details/?"
            + urlencode({"id": app_id, "pid": pid, "title": title})
        )

    def _document_name_for_text(self, text: Any) -> str:
        lowered = normalize_key(text).lower()
        if not lowered:
            return ""
        for name, patterns in self.REQUIRED_DOCUMENTS.items():
            if any(re.search(pattern, lowered, flags=re.I) for pattern in patterns):
                return name
        return ""

    def _supporting_docs_anchor(self, base_url: str) -> str:
        base_url = normalize_key(base_url)
        if not base_url:
            return ""
        return base_url.split("#", 1)[0] + "#supporting-documentation"

    def _is_static_asset_url(self, url: str) -> bool:
        lowered = normalize_key(url).lower().split("?", 1)[0]
        return lowered.endswith(
            (
                ".css",
                ".js",
                ".mjs",
                ".map",
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".svg",
                ".webp",
                ".ico",
                ".woff",
                ".woff2",
                ".ttf",
                ".eot",
            )
        )

    def _is_meaningful_document_url(self, candidate: str, base_url: str) -> bool:
        """Return True only for a plausible direct document endpoint.

        Toronto's legacy AIC ``folderRsn`` route and the current Application
        Details page are navigation pages, not files.  A ``data-id`` token is
        likewise not a URL.  Keeping this test strict prevents a folder page
        from being reported as a Civil/Utilities document.
        """
        candidate = normalize_key(candidate)
        if not candidate:
            return False
        lowered = candidate.lower()
        if lowered.startswith(("blob:", "about:", "javascript:", "mailto:", "tel:", "#")):
            return False

        full_url = urljoin(base_url, candidate)
        if self._is_static_asset_url(full_url):
            return False

        parsed = urlparse(full_url)
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        query = parsed.query.lower()

        # Never report Toronto application/folder navigation as a document.
        if host in {"app.toronto.ca", "secure.toronto.ca"} and "/aic/" in path:
            return False
        if "/aic/" in path and "folderrsn=" in query:
            return False
        if "/application-details/" in path:
            return False

        base_no_hash = normalize_key(base_url).split("#", 1)[0]
        full_no_hash = full_url.split("#", 1)[0]
        if base_no_hash and full_no_hash == base_no_hash:
            return False

        # Require file/download evidence.  Opaque UI tokens are handled by
        # clicking their actual Download buttons, never by URL construction.
        if re.search(r"\.(?:pdf|zip|docx?|xlsx?|dwg|dxf)(?:$|[?&])", path + ("?" + query if query else ""), re.I):
            return True
        return bool(re.search(r"(?:download|attachment|document|file)", path + "?" + query, re.I))

    def _download_links_fixture_html(
        self,
        document_links: dict[str, str],
        available_document_names: Iterable[str] | None = None,
        base_url: str = "",
    ) -> str:
        rows: list[str] = []
        seen: set[str] = set()
        for name, href in (document_links or {}).items():
            if not href:
                continue
            rows.append(
                f'<tr data-captured-document="true"><td>{html.escape(name)}</td>'
                f'<td><a href="{html.escape(href, quote=True)}">Download</a></td></tr>'
            )
            seen.add(name)

        # Last-resort but important: the Toronto UI sometimes hides the real
        # download URL behind JavaScript/blob handling. When we can prove the
        # required row exists, expose the application details page instead of
        # incorrectly reporting "Not found". The notification then still gives
        # the user the correct page and row to search/click.
        fallback_href = self._supporting_docs_anchor(base_url)
        for name in sorted(set(available_document_names or []), key=lambda x: list(self.REQUIRED_DOCUMENTS).index(x) if x in self.REQUIRED_DOCUMENTS else 999):
            if name in seen or not fallback_href:
                continue
            rows.append(
                f'<tr data-captured-document="available-on-page"><td>{html.escape(name)}</td>'
                f'<td><a href="{html.escape(fallback_href, quote=True)}">Available in Supporting Documentation</a></td></tr>'
            )
            seen.add(name)

        if not rows:
            return ""
        return "\n<div id=\"captured-toronto-document-links\"><table><tbody>" + "".join(rows) + "</tbody></table></div>\n"

    def _application_form_text_fixture_html(self, text: str) -> str:
        text = str(text or "")
        if not normalize_key(text):
            return ""
        return (
            "\n<div id=\"captured-toronto-application-form-text\" data-captured=\"true\">"
            "<pre>" + html.escape(text) + "</pre></div>\n"
        )

    def _fetch_rendered_page(self, url: str) -> tuple[str, str]:
        """Render one Toronto application using the proven crawler lifecycle.

        The outer City page can finish ``DOMContentLoaded`` while its planning
        application service is still showing only a Loading shell.  This method
        waits for application-specific content, uses a persistent native
        Playwright Chromium profile, expands Supporting Documentation directly,
        and never replays the legacy document API as a fallback.
        """
        if not self.config.get("render_with_playwright", True):
            raise RuntimeError(
                "Toronto application details are JavaScript-rendered; "
                "render_with_playwright must remain enabled."
            )

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError(
                "Playwright is not installed. Install it and Chromium with: "
                "pip install playwright && python -m playwright install chromium"
            ) from exc

        diagnostics: dict[str, Any] = {"requested_url": url}
        with sync_playwright() as playwright:
            context, launch_info = self._launch_toronto_context(playwright)
            diagnostics["browser_launch"] = launch_info
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(
                int(self.config.get("playwright_default_timeout_ms", 2500) or 2500)
            )
            page.set_default_navigation_timeout(
                int(self.config.get("page_timeout_ms", 60000) or 60000)
            )
            network_recorder = self._install_toronto_network_recorder(context)

            try:
                service_state = self._open_and_wait_for_application(page, url)
                final_url = normalize_key(page.url) or url
                diagnostics["application_service_state"] = service_state
                diagnostics["final_url"] = final_url

                if service_state != "ready":
                    reason = (
                        "maintenance_response"
                        if service_state == "maintenance"
                        else "application_service_timeout"
                    )
                    debug_path = self._save_toronto_debug_artifacts(
                        page, network_recorder, diagnostics, reason
                    )
                    raise RuntimeError(
                        "Toronto's JavaScript application service did not become ready "
                        f"after {int(self.config.get('application_service_retries', 3) or 3)} "
                        f"attempt(s); final state={service_state}. "
                        f"Debug artifacts: {debug_path or 'not saved'}"
                    )

                self._log_toronto_browser_identity(page)
                diagnostics["widget_probe"] = self._application_widget_probe(page)
                mount_probe = self._scroll_until_supporting_docs_mounted(page)
                diagnostics["supporting_docs_mount_probe"] = mount_probe

                section_state = self._expand_supporting_documentation(page)
                diagnostics["supporting_documentation_state"] = section_state
                if not section_state.get("open"):
                    debug_path = self._save_toronto_debug_artifacts(
                        page,
                        network_recorder,
                        diagnostics,
                        "supporting_documentation_not_expanded",
                    )
                    raise RuntimeError(
                        "Toronto Supporting Documentation did not expand or confirm an "
                        f"empty table. Debug artifacts: {debug_path or 'not saved'}"
                    )

                for scope in self._document_scopes(page):
                    self._set_document_table_to_all_rows(scope)
                try:
                    page.wait_for_timeout(800)
                except Exception:
                    pass

                button_links, button_available = self._wait_for_toronto_download_rows(
                    page, final_url
                )

                # Search the mounted DataTable for required categories that may
                # be on another page.  This only interrogates the proven DOM; it
                # does not replay arbitrary XHR/fetch calls.
                missing_names = [
                    name for name in self.REQUIRED_DOCUMENTS if name not in button_available
                ]
                search_terms = {
                    "Application Form": ["Application Form", "Application"],
                    "Architectural Plans": [
                        "Architectural Plans", "Architectural", "Floor Plan", "Elevation"
                    ],
                    "Civil and Utilities Plans": [
                        "Civil and Utilities", "Civil", "Utilities", "Servicing",
                        "Grading", "Stormwater"
                    ],
                    "Geotechnical Study": ["Geotechnical Study", "Geotechnical", "Soil"],
                    "Hydrogeological Report": [
                        "Hydrogeological Report", "Hydrogeological", "Hydrogeology",
                        "Groundwater", "Dewatering"
                    ],
                }
                for required_name in missing_names:
                    for term in search_terms.get(required_name, [required_name]):
                        for scope in self._document_scopes(page):
                            self._set_document_table_to_all_rows(scope, term)
                        more_links, more_available = self._extract_download_button_document_rows(
                            page, final_url
                        )
                        button_available.update(more_available)
                        for name, href in more_links.items():
                            if href and not button_links.get(name):
                                button_links[name] = href
                        if required_name in button_available:
                            break

                for scope in self._document_scopes(page):
                    self._set_document_table_to_all_rows(scope)

                visible_available = self._document_names_visible_on_page(page)
                document_links = {
                    name: href
                    for name, href in button_links.items()
                    if self._is_meaningful_document_url(href, final_url)
                }
                available_names = (
                    set(button_available)
                    | set(visible_available)
                    | set(document_links)
                )

                html_text = self._combined_page_content(page)
                html_text += self._download_links_fixture_html(
                    document_links, available_names, final_url
                )

                application_form_text = ""
                if (
                    self.config.get("extract_application_form_contacts", True)
                    and "Application Form" in available_names
                ):
                    application_form_text = self._extract_application_form_text_from_page(
                        page, final_url
                    )
                    if normalize_key(application_form_text):
                        html_text += self._application_form_text_fixture_html(
                            application_form_text
                        )
                    else:
                        html_text += (
                            '\n<div id="toronto-enrichment-status" '
                            'data-status="application-form-contacts-not-extracted"></div>\n'
                        )

                final_probe = self._supporting_docs_probe(page)
                diagnostics.update(
                    {
                        "final_supporting_docs_probe": final_probe,
                        "available_document_names": sorted(available_names),
                        "direct_document_links": sorted(document_links),
                        "application_form_text_chars": len(application_form_text or ""),
                        "document_api_fallback_used": False,
                    }
                )

                LOGGER.info(
                    "Supporting Documentation for %s: direct_links=%s; available_rows=%s; "
                    "application_form_text=%s; names=%s",
                    final_url,
                    len(document_links),
                    len(available_names),
                    bool(normalize_key(application_form_text)),
                    ", ".join(sorted(available_names)) or "none",
                )
                return final_url, html_text
            except Exception:
                try:
                    self._save_toronto_debug_artifacts(
                        page, network_recorder, diagnostics, "render_exception"
                    )
                except Exception:
                    pass
                raise
            finally:
                try:
                    context.close()
                except Exception:
                    pass

    def _launch_toronto_context(self, playwright: Any) -> tuple[Any, dict[str, Any]]:
        """Launch Playwright's bundled Chromium with one persistent native profile."""
        profile_dir = Path(
            self.config.get("browser_profile_dir")
            or self.config.get("playwright_profile_dir")
            or "data/toronto_browser_profile"
        )
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Remove stale Chromium singleton files left by a cancelled CI job.
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (profile_dir / name).unlink(missing_ok=True)
            except Exception:
                pass

        args = ["--disable-dev-shm-usage"]
        if os.name != "nt":
            args.append("--no-sandbox")

        browser_channel = normalize_key(
            self.config.get("browser_channel") or "chromium"
        ).lower()
        kwargs: dict[str, Any] = {
            "headless": not bool(self.config.get("headed", False)),
            "accept_downloads": True,
            "locale": "en-CA",
            "timezone_id": "America/Toronto",
            "viewport": {"width": 1440, "height": 1000},
            "args": args,
        }
        if browser_channel in {"chrome", "msedge"}:
            kwargs["channel"] = browser_channel
            label = f"system {browser_channel}"
        else:
            # channel omitted means the Chromium binary versioned with this
            # exact Playwright installation.  Do not override userAgent or
            # silently substitute another executable.
            label = "Playwright-bundled Chromium"
            LOGGER.info("Bundled Chromium executable: %s", playwright.chromium.executable_path)

        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir.resolve()),
                **kwargs,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not launch {label} with persistent profile {profile_dir}: {exc}"
            ) from exc

        return context, {
            "mode": "persistent-context",
            "browser": label,
            "profile_dir": str(profile_dir),
            "native_user_agent": True,
        }

    def _application_service_state(self, page: Any) -> str:
        """Return ready, loading, maintenance, or unknown for Toronto's JS app."""
        saw_loading = False
        saw_maintenance = False

        for scope in self._document_scopes(page):
            try:
                body = normalize_key(scope.locator("body").inner_text(timeout=900))
            except Exception:
                continue

            lower = body.lower()
            if (
                "we are currently performing maintenance" in lower
                or ("application is not available" in lower and "try again later" in lower)
            ):
                saw_maintenance = True
            if re.search(r"\bloading(?:\.{0,3})?\b", lower):
                saw_loading = True

            has_supporting = "supporting documentation" in lower
            has_application_data = any(
                token in lower
                for token in (
                    "milestone status",
                    "application submitted",
                    "application details url",
                    "related applications",
                    "view all properties",
                    "application status",
                    "application number",
                    "reference file",
                )
            )
            if has_supporting and has_application_data and not saw_maintenance:
                return "ready"

        if saw_maintenance:
            return "maintenance"
        if saw_loading:
            return "loading"
        return "unknown"

    def _open_and_wait_for_application(self, page: Any, url: str) -> str:
        """Navigate with bounded retries until the application service is stable."""
        retries = max(1, int(self.config.get("application_service_retries", 3) or 3))
        timeout_seconds = max(
            15.0,
            float(self.config.get("application_service_timeout_seconds", 90) or 90),
        )
        poll_ms = max(250, int(self.config.get("application_service_poll_ms", 750) or 750))
        confirmations = max(
            1,
            int(self.config.get("application_service_ready_confirmations", 2) or 2),
        )
        final_state = "unknown"

        for attempt in range(1, retries + 1):
            if attempt == 1:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.config.get("page_timeout_ms", 60000) or 60000),
                )
            else:
                delay_ms = min(10000, 1500 * (2 ** (attempt - 2)))
                LOGGER.info(
                    "Retrying Toronto application service in %.1f second(s) "
                    "(attempt %d/%d)",
                    delay_ms / 1000,
                    attempt,
                    retries,
                )
                page.wait_for_timeout(delay_ms)
                page.reload(
                    wait_until="domcontentloaded",
                    timeout=int(self.config.get("page_timeout_ms", 60000) or 60000),
                )

            deadline = time.monotonic() + timeout_seconds
            last_reported = ""
            ready_polls = 0
            while time.monotonic() < deadline:
                state = self._application_service_state(page)
                final_state = state
                if state == "ready":
                    ready_polls += 1
                    if ready_polls >= confirmations:
                        LOGGER.info(
                            "Toronto application service ready on attempt %d/%d",
                            attempt,
                            retries,
                        )
                        return "ready"
                else:
                    ready_polls = 0

                if state != last_reported:
                    LOGGER.info(
                        "Waiting for Toronto application service: %s (attempt %d/%d)",
                        state,
                        attempt,
                        retries,
                    )
                    last_reported = state

                if state == "maintenance":
                    break
                try:
                    page.mouse.wheel(0, 500)
                except Exception:
                    pass
                page.wait_for_timeout(poll_ms)

            LOGGER.warning(
                "Toronto application service wait expired on attempt %d/%d; last state=%s",
                attempt,
                retries,
                final_state,
            )

        return final_state

    def _log_toronto_browser_identity(self, page: Any) -> None:
        """Log the native browser identity without overriding/spoofing it."""
        try:
            identity = page.evaluate(
                """async () => {
                    const uaData = navigator.userAgentData;
                    let high = null;
                    if (uaData && uaData.getHighEntropyValues) {
                        try {
                            high = await uaData.getHighEntropyValues([
                                'architecture', 'bitness', 'fullVersionList',
                                'model', 'platformVersion', 'wow64'
                            ]);
                        } catch (e) {}
                    }
                    return {
                        userAgent: navigator.userAgent,
                        brands: uaData ? uaData.brands : null,
                        mobile: uaData ? uaData.mobile : null,
                        platform: uaData ? uaData.platform : navigator.platform,
                        language: navigator.language,
                        languages: navigator.languages,
                        webdriver: navigator.webdriver,
                        highEntropy: high
                    };
                }"""
            )
            LOGGER.info(
                "Native browser identity (not overridden): %s",
                json.dumps(identity, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:
            LOGGER.debug("Could not log browser identity: %s", exc)

    def _application_widget_probe(self, page: Any) -> dict[str, Any]:
        """Probe all relevant scopes, including open shadow roots."""
        details: list[dict[str, Any]] = []
        marker_count = 0
        strong_marker_count = 0
        js = r"""
        () => {
            const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const roots = [];
            const seen = new Set();
            const addRoot = root => {
                if (!root || seen.has(root)) return;
                seen.add(root); roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);
            const parts = [];
            for (const root of roots) {
                try { parts.push(clean(root.innerText || root.textContent || '')); } catch (e) {}
            }
            const text = clean(parts.join(' '));
            const markers = {
                applicationNumber: /Application\s+Number|Reference\s+File/i.test(text),
                applicationStatus: /Application\s+Status/i.test(text),
                supportingDocumentation: /Supporting\s+Documentation/i.test(text),
                referenceFile: /Reference\s+File/i.test(text),
                proposal: /Proposal|Application\s+Description/i.test(text),
                numberedApplication: /\b\d{2}\s+\d{5,7}\b/.test(text),
            };
            return {
                url: location.href,
                title: document.title || '',
                textLength: text.length,
                loadingOnly: /^\s*Loading\s*$/i.test(text) || (text.length < 250 && /Loading/i.test(text)),
                markers,
                iframeCount: document.querySelectorAll('iframe').length,
                sample: text.slice(0, 800),
            };
        }
        """
        for scope in self._document_scopes(page):
            try:
                result = scope.evaluate(js) or {}
            except Exception as exc:
                details.append({"url": normalize_key(getattr(scope, "url", "")), "error": shorten(str(exc), 250)})
                continue
            markers = result.get("markers") if isinstance(result.get("markers"), dict) else {}
            count = sum(1 for value in markers.values() if value)
            marker_count += count
            strong_marker_count += sum(
                1 for key in ("applicationNumber", "applicationStatus", "supportingDocumentation", "referenceFile")
                if markers.get(key)
            )
            details.append(result)
        ready = strong_marker_count >= 1 or marker_count >= 2
        return {
            "ready": ready,
            "marker_count": marker_count,
            "strong_marker_count": strong_marker_count,
            "scope_count": len(details),
            "scopes": details,
        }


    def _safe_debug_slug(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", normalize_key(value)).strip("_")
        return slug[:80] or "toronto_application"

    def _network_debug_snapshot(self, recorder: dict[str, Any]) -> dict[str, Any]:
        """Return useful network diagnostics without persisting session secrets."""
        snapshot: dict[str, Any] = {"requests": [], "responses": [], "download_urls": []}
        if not isinstance(recorder, dict):
            return snapshot

        for item in (recorder.get("requests") or [])[:500]:
            if not isinstance(item, dict):
                continue
            headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
            debug_headers: dict[str, str] = {}
            for key, value in headers.items():
                lowered = str(key).lower()
                if lowered in {"authorization", "x-xsrf-token", "x-csrf-token", "cookie"}:
                    debug_headers[lowered] = "[REDACTED]"
                elif lowered in {"accept", "content-type", "origin", "referer", "x-requested-with"}:
                    debug_headers[lowered] = str(value)
            post_data = item.get("post_data")
            snapshot["requests"].append({
                "url": item.get("url"),
                "method": item.get("method"),
                "resource_type": item.get("resource_type"),
                "content_type": item.get("content_type"),
                "post_data_chars": len(str(post_data)) if post_data not in (None, "") else 0,
                "headers": debug_headers,
            })

        allowed_response_headers = {
            "content-type", "content-disposition", "content-length",
            "location", "cache-control", "etag", "last-modified",
        }
        for item in (recorder.get("responses") or [])[:500]:
            if not isinstance(item, dict):
                continue
            headers = item.get("headers") if isinstance(item.get("headers"), dict) else {}
            snapshot["responses"].append({
                "url": item.get("url"),
                "status": item.get("status"),
                "resource_type": item.get("resource_type"),
                "headers": {
                    str(k).lower(): str(v)
                    for k, v in headers.items()
                    if str(k).lower() in allowed_response_headers
                },
            })

        snapshot["download_urls"] = [
            normalize_key(value) for value in (recorder.get("download_urls") or [])[:200] if normalize_key(value)
        ]
        return snapshot

    def _save_toronto_debug_artifacts(
        self,
        page: Any,
        recorder: dict[str, Any],
        diagnostics: dict[str, Any],
        reason: str,
    ) -> str:
        if not self.config.get("save_debug_artifacts_on_failure", True):
            return ""
        root = self.config.get("debug_artifacts_dir", "data/toronto_debug")
        timestamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
        slug = self._safe_debug_slug(urlparse(normalize_key(getattr(page, "url", ""))).query or reason)
        out_dir = os.path.abspath(os.path.join(root, f"{timestamp}_{slug}_{self._safe_debug_slug(reason)}"))
        os.makedirs(out_dir, exist_ok=True)

        payload = dict(diagnostics or {})
        payload["reason"] = reason
        payload["page_url"] = normalize_key(getattr(page, "url", ""))
        payload["network"] = self._network_debug_snapshot(recorder)
        try:
            payload["final_widget_probe"] = self._application_widget_probe(page)
            payload["final_supporting_docs_probe"] = self._supporting_docs_probe(page)
        except Exception:
            pass
        with open(os.path.join(out_dir, "diagnostics.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)

        try:
            page.screenshot(path=os.path.join(out_dir, "page.png"), full_page=True, timeout=8000)
        except Exception as exc:
            LOGGER.info("Could not save Toronto debug screenshot: %s", exc)
        try:
            with open(os.path.join(out_dir, "main_page.html"), "w", encoding="utf-8") as fh:
                fh.write(page.content())
        except Exception:
            pass
        for index, scope in enumerate(self._document_scopes(page)):
            try:
                with open(os.path.join(out_dir, f"scope_{index}.html"), "w", encoding="utf-8") as fh:
                    fh.write(scope.content())
            except Exception:
                continue
        LOGGER.warning("Toronto debug artifacts saved to %s", out_dir)
        return out_dir


    def _document_scopes(self, page: Any) -> list[Any]:
        """Return the main page plus a bounded set of non-analytics child frames.

        The Toronto widget host/domain has changed more than once.  Requiring a
        frame URL to contain words such as ``aic`` or ``application`` can silently
        exclude the real widget.  Exclude only known noise frames and inspect up
        to eight remaining frames.
        """
        scopes: list[Any] = [page]
        if not self.config.get("inspect_child_frames", True):
            return scopes

        deny_tokens = (
            "googletagmanager", "google-analytics", "doubleclick", "medallia",
            "youtube", "facebook", "twitter", "translate.google", "recaptcha",
            "qualtrics", "hotjar",
        )
        try:
            frames = list(page.frames)
        except Exception:
            return scopes

        for frame in frames:
            if len(scopes) >= 9:
                break
            try:
                if frame is page.main_frame or frame.is_detached():
                    continue
                frame_url = normalize_key(frame.url).lower()
            except Exception:
                continue
            if any(token in frame_url for token in deny_tokens):
                continue
            scopes.append(frame)
        return scopes

    def _combined_page_content(self, page: Any) -> str:
        parts: list[str] = []
        for scope in self._document_scopes(page):
            try:
                parts.append(scope.content())
            except Exception:
                continue
        return "\n".join(parts)

    def _supporting_docs_probe(self, page: Any) -> dict[str, int]:
        """Cheap aggregate probe across the page and relevant application frames."""
        totals = {
            "downloadButtons": 0,
            "supportingTextMatches": 0,
            "referenceFileMatches": 0,
            "documentRows": 0,
        }
        js = r"""
        () => {
            const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const roots = [];
            const seenRoots = new Set();
            const addRoot = root => {
                if (!root || seenRoots.has(root)) return;
                seenRoots.add(root);
                roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);
            const all = selector => {
                const out = [];
                const seen = new Set();
                for (const root of roots) {
                    let nodes = [];
                    try { nodes = Array.from(root.querySelectorAll(selector)); } catch (e) { nodes = []; }
                    for (const node of nodes) if (!seen.has(node)) { seen.add(node); out.push(node); }
                }
                return out;
            };
            const bodyText = clean(document.body ? document.body.innerText || document.body.textContent || '' : '');
            const rows = all('tr, [role="row"]');
            return {
                downloadButtons: all('button.downloadFile[data-id], .downloadFile[data-id]').length,
                supportingTextMatches: /Supporting\s+Documentation/i.test(bodyText) ? 1 : 0,
                referenceFileMatches: /Reference\s+File/i.test(bodyText) ? 1 : 0,
                documentRows: rows.filter(tr => /Application Form|Architectural|Civil|Utilities|Geotechnical|Hydrogeological|Hydrogeology/i.test(clean(tr.innerText || tr.textContent || ''))).length,
            };
        }
        """
        for scope in self._document_scopes(page):
            try:
                result = scope.evaluate(js) or {}
            except Exception:
                continue
            for key in totals:
                totals[key] += int(result.get(key) or 0)
        return totals

    def _scroll_until_supporting_docs_mounted(self, page: Any, max_seconds: float | None = None) -> dict[str, int]:
        """Scroll the main document and relevant frames until the lazy section mounts."""
        max_seconds = float(max_seconds or self.config.get("supporting_docs_mount_wait_seconds", 9) or 9)
        deadline = time.monotonic() + max_seconds
        best = self._supporting_docs_probe(page)
        step = 0
        while time.monotonic() < deadline:
            if best.get("downloadButtons") or best.get("documentRows") or best.get("referenceFileMatches"):
                break
            step += 1
            for scope in self._document_scopes(page):
                try:
                    scope.evaluate(
                        """
                        () => {
                            const root = document.scrollingElement || document.documentElement || document.body;
                            const vh = window.innerHeight || 900;
                            if (root) root.scrollTop = Math.min(root.scrollHeight, (root.scrollTop || 0) + Math.max(650, Math.floor(vh * 0.85)));
                            window.scrollBy(0, Math.max(650, Math.floor(vh * 0.85)));
                        }
                        """
                    )
                except Exception:
                    continue
            try:
                page.wait_for_timeout(400)
            except Exception:
                break
            best = self._supporting_docs_probe(page)
            if step in {1, 4, 8}:
                LOGGER.info("Toronto Supporting Documentation mount probe step %s: %s", step, best)

        for scope in self._document_scopes(page):
            try:
                scope.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
        LOGGER.info("Toronto Supporting Documentation mount probe final: %s", best)
        return best

    def _expand_supporting_documentation(self, page: Any) -> dict[str, Any]:
        """Open only Supporting Documentation and verify its resulting state."""
        for attempt in range(1, 4):
            state = self._supporting_documentation_state(page)
            if state.get("open"):
                return state

            clicked = self._click_supporting_docs_with_playwright_locators(page)
            LOGGER.info(
                "Toronto Supporting Documentation targeted click attempt %d: clicked=%d",
                attempt,
                clicked,
            )
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass

            state = self._supporting_documentation_state(page)
            if state.get("open"):
                return state

            # Continue progressive scrolling so a lazily mounted lower panel can
            # initialize before the next exact targeted click.
            for scope in self._document_scopes(page):
                try:
                    scope.evaluate(
                        """() => {
                            const root = document.scrollingElement || document.documentElement || document.body;
                            if (root) root.scrollTop = Math.min(root.scrollHeight, (root.scrollTop || 0) + 900);
                            window.scrollBy(0, 900);
                        }"""
                    )
                except Exception:
                    continue

        return self._supporting_documentation_state(page)

    def _supporting_documentation_state(self, page: Any) -> dict[str, Any]:
        """Return whether the targeted accordion exists and is genuinely open."""
        totals = {
            "found": False,
            "expanded": False,
            "empty": False,
            "visible_rows": 0,
            "open": False,
        }
        js = r"""
        () => {
            const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const visible = el => {
                try {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                } catch (e) { return false; }
            };
            const roots = [];
            const seenRoots = new Set();
            const addRoot = root => {
                if (!root || seenRoots.has(root)) return;
                seenRoots.add(root); roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);

            let found = false;
            let expanded = false;
            let empty = false;
            let visibleRows = 0;

            for (const root of roots) {
                let rows = [];
                try {
                    rows = Array.from(root.querySelectorAll(
                        'tr:has(button.downloadFile[data-id]), tr:has(.downloadFile[data-id]), '
                        + '[role="row"]:has(button.downloadFile[data-id]), [role="row"]:has(.downloadFile[data-id])'
                    ));
                } catch (e) { rows = []; }
                visibleRows += rows.filter(visible).length;

                let controls = [];
                try {
                    controls = Array.from(root.querySelectorAll(
                        'button, a, summary, [role="button"], [aria-controls], .accordion-button, .accordion-header'
                    ));
                } catch (e) { controls = []; }
                for (const control of controls) {
                    const text = clean(control.innerText || control.textContent || control.getAttribute('aria-label') || '');
                    if (!/\bSupporting Documentation\b/i.test(text)) continue;
                    found = true;
                    const aria = clean(control.getAttribute('aria-expanded')).toLowerCase();
                    const cls = clean(control.className).toLowerCase();
                    if (aria === 'true' && !/collapsed/.test(cls)) expanded = true;
                    const panelId = clean(control.getAttribute('aria-controls'));
                    if (panelId) {
                        try {
                            const panel = root.getElementById ? root.getElementById(panelId) : root.querySelector(`[id="${CSS.escape(panelId)}"]`);
                            if (panel && visible(panel)) {
                                expanded = true;
                                const panelText = clean(panel.innerText || panel.textContent || '');
                                if (/No data available|No matching records|No supporting documents|0 entries/i.test(panelText)) empty = true;
                            }
                        } catch (e) {}
                    }
                }

                let bodyText = '';
                try { bodyText = clean(root.innerText || root.textContent || ''); } catch (e) {}
                if (/Supporting Documentation/i.test(bodyText)
                    && /No data available|No matching records|No supporting documents/i.test(bodyText)) {
                    empty = true;
                }
            }
            return {
                found,
                expanded,
                empty,
                visible_rows: visibleRows,
                open: visibleRows > 0 || expanded || (found && empty),
            };
        }
        """
        for scope in self._document_scopes(page):
            try:
                result = scope.evaluate(js) or {}
            except Exception:
                continue
            totals["found"] = bool(totals["found"] or result.get("found"))
            totals["expanded"] = bool(totals["expanded"] or result.get("expanded"))
            totals["empty"] = bool(totals["empty"] or result.get("empty"))
            totals["visible_rows"] += int(result.get("visible_rows") or 0)

        totals["open"] = bool(
            totals["visible_rows"] > 0
            or totals["expanded"]
            or (totals["found"] and totals["empty"])
        )
        return totals

    def _click_supporting_docs_with_playwright_locators(self, page: Any) -> int:
        """Click one actionable Supporting Documentation control per scope.

        Page-wide ``Expand All`` is intentionally excluded because Toronto can
        expand every other table while leaving Supporting Documentation closed.
        """
        js = r"""
        () => {
            const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const visible = el => {
                try {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.width > 0 && rect.height > 0;
                } catch (e) { return false; }
            };
            const roots = [];
            const seenRoots = new Set();
            const addRoot = root => {
                if (!root || seenRoots.has(root)) return;
                seenRoots.add(root); roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);

            for (const root of roots) {
                let labels = [];
                try {
                    labels = Array.from(root.querySelectorAll(
                        'button, a, summary, [role="button"], [aria-controls], .accordion-button, .accordion-header, h2, h3, h4'
                    ));
                } catch (e) { labels = []; }
                for (const label of labels) {
                    const text = clean(label.innerText || label.textContent || label.getAttribute('aria-label') || '');
                    if (!/\bSupporting Documentation\b/i.test(text) || /Expand All/i.test(text)) continue;

                    let target = label;
                    if (!/^(BUTTON|A|SUMMARY)$/i.test(target.tagName || '')
                        && clean(target.getAttribute('role')).toLowerCase() !== 'button') {
                        const nested = target.querySelector && target.querySelector(
                            'button, a, summary, [role="button"], [aria-controls]'
                        );
                        if (nested) target = nested;
                        else if (target.closest) {
                            const ancestor = target.closest(
                                'button, a, summary, [role="button"], [aria-controls]'
                            );
                            if (ancestor) target = ancestor;
                        }
                    }

                    const expanded = clean(target.getAttribute('aria-expanded')).toLowerCase();
                    const cls = clean(target.className).toLowerCase();
                    if (expanded === 'true' && !/collapsed/.test(cls)) return 0;
                    if (!visible(target) && !visible(label)) continue;
                    try { target.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                    try { target.click(); return 1; } catch (e) {}
                }
            }
            return 0;
        }
        """
        clicked_total = 0
        for scope in self._document_scopes(page):
            try:
                clicked_total += int(scope.evaluate(js) or 0)
            except Exception:
                continue
        return clicked_total

    def _force_supporting_docs_visible(self, page: Any) -> None:
        """Compatibility helper that performs only the safe targeted click."""
        self._click_supporting_docs_with_playwright_locators(page)
        try:
            page.wait_for_timeout(900)
        except Exception:
            pass

    def _wait_for_toronto_download_rows(self, page: Any, base_url: str) -> tuple[dict[str, str], set[str]]:
        """Bounded wait for exact button.downloadFile[data-id] rows.

        The previous version repeatedly forced accordions/tables and inspected
        detached frames, which could hold a GitHub Actions job for many minutes.
        This version has a hard small cap and exits early when the document UI
        has not mounted at all.
        """
        timeout_seconds = float(self.config.get("document_rows_wait_seconds", 8) or 8)
        deadline = time.monotonic() + timeout_seconds
        best_links: dict[str, str] = {}
        best_available: set[str] = set()
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            try:
                self._set_document_table_to_all_rows(page)
            except Exception:
                pass

            links, available = self._extract_download_button_document_rows(page, base_url)
            best_available.update(available)
            for name, href in links.items():
                if href and not best_links.get(name):
                    best_links[name] = href

            if "Application Form" in best_available or len(best_available) >= 2:
                LOGGER.info(
                    "Toronto exact document rows ready for %s after %s attempt(s): %s",
                    base_url,
                    attempt,
                    ", ".join(sorted(best_available)) or "none",
                )
                return best_links, best_available

            probe = self._supporting_docs_probe(page)
            if not (probe.get("downloadButtons") or probe.get("supportingTextMatches") or probe.get("referenceFileMatches")):
                # No document UI exists in this browser session; stop burning time.
                LOGGER.info("Toronto document row wait stopping early for %s; document UI not mounted: %s", base_url, probe)
                break

            try:
                page.wait_for_timeout(1000)
            except Exception:
                break

        LOGGER.info(
            "Toronto exact document rows not found for %s after %.1fs; best=%s",
            base_url,
            timeout_seconds,
            ", ".join(sorted(best_available)) or "none",
        )
        return best_links, best_available


    def _install_toronto_network_recorder(self, page: Any) -> dict[str, Any]:
        """Record bounded request and response metadata for safe API replay."""
        recorder: dict[str, Any] = {"requests": [], "responses": [], "download_urls": []}

        def on_request(request: Any) -> None:
            try:
                url = normalize_key(request.url)
                if self._is_static_asset_url(url):
                    return
                headers = {str(k).lower(): str(v) for k, v in (request.headers or {}).items()}
                safe_header_names = {
                    "accept", "content-type", "origin", "referer",
                    "x-requested-with", "x-xsrf-token", "x-csrf-token",
                    "authorization",
                }
                replay_headers = {k: v for k, v in headers.items() if k in safe_header_names and v}
                item = {
                    "url": url,
                    "method": normalize_key(request.method),
                    "resource_type": normalize_key(request.resource_type),
                    "post_data": request.post_data,
                    "content_type": normalize_key(headers.get("content-type", "")),
                    "headers": replay_headers,
                }
                rows = recorder.setdefault("requests", [])
                if len(rows) < 500:
                    rows.append(item)
            except Exception:
                return

        def on_response(response: Any) -> None:
            try:
                headers = {k.lower(): v for k, v in (response.headers or {}).items()}
                url = normalize_key(response.url)
                request = getattr(response, "request", None)
                resource_type = normalize_key(getattr(request, "resource_type", "")) if request else ""
                item = {
                    "url": url,
                    "status": getattr(response, "status", None),
                    "headers": headers,
                    "resource_type": resource_type,
                }
                responses = recorder.setdefault("responses", [])
                if len(responses) < 500:
                    responses.append(item)
                if self._response_looks_like_download(url, headers):
                    recorder.setdefault("download_urls", []).append(url)
            except Exception:
                return

        try:
            page.on("request", on_request)
            page.on("response", on_response)
        except Exception:
            pass
        return recorder

    def _response_looks_like_download(self, url: str, headers: dict[str, str]) -> bool:
        content_type = normalize_key(headers.get("content-type", "")).lower()
        content_disposition = normalize_key(headers.get("content-disposition", "")).lower()
        lowered_url = normalize_key(url).lower()
        if "attachment" in content_disposition:
            return True
        if any(
            token in content_type
            for token in (
                "application/pdf",
                "application/octet-stream",
                "application/zip",
                "application/x-zip",
                "application/msword",
                "application/vnd.",
            )
        ):
            return True
        return bool(re.search(r"\.(pdf|zip|docx?|xlsx?)(?:\?|$)", lowered_url))




    def _extract_download_button_document_rows(self, page: Any, base_url: str) -> tuple[dict[str, str], set[str]]:
        """Read Supporting Documentation rows in one bounded operation per scope.

        The first cell identifies the document.  A ``data-id`` proves that a
        Download control exists, but is deliberately *not* returned as a URL.
        Only a genuine href/file endpoint is placed in ``links``.
        """
        links: dict[str, str] = {}
        available: set[str] = set()
        rows_seen = 0
        token_rows = 0

        js = r"""
        () => {
            const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const wanted = /Application\s+Form|Architectural|Civil|Utilities|Servicing|Grading|Stormwater|Geotechnical|Geo[-\s]?tech|Hydrogeological|Hydrogeology|Groundwater|Dewatering/i;
            const roots = [];
            const seenRoots = new Set();
            const addRoot = root => {
                if (!root || seenRoots.has(root)) return;
                seenRoots.add(root);
                roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);

            const out = [];
            const seenRows = new Set();
            const addRow = (source, firstCell, rowText, hrefs, dataIds) => {
                const first = clean(firstCell);
                const text = clean(rowText);
                if (!wanted.test(first || text)) return;
                const key = [first, text, ...(hrefs || []), ...(dataIds || [])].join('|');
                if (seenRows.has(key) || out.length >= 300) return;
                seenRows.add(key);
                out.push({source, first_cell: first, text, hrefs: hrefs || [], data_ids: dataIds || []});
            };
            const attrsFromNode = node => {
                const hrefs = [];
                const dataIds = [];
                const add = (arr, value) => {
                    const v = clean(value);
                    if (v && !arr.includes(v)) arr.push(v);
                };
                let nodes = [];
                try { nodes = [node, ...node.querySelectorAll('a[href], [href], [src], [formaction], [data-url], [data-href], [data-download-url], [data-document-url], [data-file-url], button.downloadFile[data-id], .downloadFile[data-id]')]; } catch (e) { nodes = [node]; }
                for (const el of nodes) {
                    if (!el || !el.getAttribute) continue;
                    add(dataIds, el.getAttribute('data-id'));
                    for (const attr of ['href','src','formaction','data-url','data-href','data-download-url','data-document-url','data-file-url']) add(hrefs, el.getAttribute(attr));
                }
                return {hrefs, dataIds};
            };
            const textFromHtml = value => {
                const div = document.createElement('div');
                div.innerHTML = String(value ?? '');
                return clean(div.innerText || div.textContent || value);
            };
            const attrsFromHtml = value => {
                const raw = String(value ?? '');
                const div = document.createElement('div');
                div.innerHTML = raw;
                const attrs = attrsFromNode(div);
                for (const match of raw.matchAll(/data-id\s*=\s*["']([^"']+)["']/gi)) if (!attrs.dataIds.includes(clean(match[1]))) attrs.dataIds.push(clean(match[1]));
                for (const match of raw.matchAll(/(?:href|src|data-(?:url|href|download-url|document-url|file-url))\s*=\s*["']([^"']+)["']/gi)) if (!attrs.hrefs.includes(clean(match[1]))) attrs.hrefs.push(clean(match[1]));
                return attrs;
            };

            for (const root of roots) {
                let tables = [];
                try { tables = Array.from(root.querySelectorAll('table')); } catch (e) { tables = []; }
                for (const table of tables.slice(0, 30)) {
                    const tableText = clean(table.innerText || table.textContent || '');
                    const tableHtml = String(table.innerHTML || '');
                    if (!/Reference\s+File|Supporting\s+Documentation|downloadFile|Application\s+Form/i.test(tableText + ' ' + tableHtml)) continue;

                    let rows = [];
                    try { rows = Array.from(table.querySelectorAll('tbody tr, tr')); } catch (e) { rows = []; }
                    for (const tr of rows.slice(0, 300)) {
                        const cells = Array.from(tr.children || []).filter(el => /^(TD|TH)$/i.test(el.tagName || ''));
                        const first = clean(cells[0]?.innerText || cells[0]?.textContent || '');
                        const text = clean(tr.innerText || tr.textContent || '');
                        const attrs = attrsFromNode(tr);
                        addRow('dom', first, text, attrs.hrefs, attrs.dataIds);
                    }

                    // DataTables may retain non-visible pages internally.  Read
                    // at most 300 records without changing pagination or drawing.
                    try {
                        if (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable && window.jQuery.fn.dataTable.isDataTable(table)) {
                            const dt = window.jQuery(table).DataTable();
                            const data = dt.rows().data().toArray().slice(0, 300);
                            for (const row of data) {
                                const cells = Array.isArray(row) ? row : (row && typeof row === 'object' ? Object.values(row) : [row]);
                                const attrs = {hrefs: [], dataIds: []};
                                for (const cell of cells) {
                                    const found = attrsFromHtml(cell);
                                    for (const href of found.hrefs) if (!attrs.hrefs.includes(href)) attrs.hrefs.push(href);
                                    for (const id of found.dataIds) if (!attrs.dataIds.includes(id)) attrs.dataIds.push(id);
                                }
                                addRow('datatable', textFromHtml(cells[0]), cells.map(textFromHtml).join(' '), attrs.hrefs, attrs.dataIds);
                            }
                        }
                    } catch (e) {}
                }
            }
            return out;
        }
        """

        for scope in self._document_scopes(page):
            try:
                raw_rows = scope.evaluate(js) or []
            except Exception as exc:
                LOGGER.info("Could not inspect Toronto document rows for %s: %s", base_url, exc)
                continue
            for row in raw_rows[:300]:
                rows_seen += 1
                if not isinstance(row, dict):
                    continue
                first_cell = normalize_key(row.get("first_cell"))
                row_text = normalize_key(row.get("text"))
                document_name = self._document_name_for_text(first_cell) or self._document_name_for_text(row_text)
                if not document_name:
                    continue
                available.add(document_name)
                if any(normalize_key(value) for value in (row.get("data_ids") or [])):
                    token_rows += 1
                for href in row.get("hrefs") or []:
                    full_url = urljoin(base_url, normalize_key(href))
                    if self._is_meaningful_document_url(full_url, base_url):
                        links.setdefault(document_name, full_url)
                        break

        LOGGER.info(
            "Toronto document-row scan for %s: rows=%d; token_rows=%d; available=%s; direct_links=%s",
            base_url,
            rows_seen,
            token_rows,
            ", ".join(sorted(available)) or "none",
            ", ".join(sorted(links)) or "none",
        )
        return links, available





    def _set_document_table_to_all_rows(self, scope: Any, search_term: str = "") -> None:
        """Search and expand only tables that look like Supporting Documentation."""
        try:
            scope.evaluate(
                r"""
                (searchTerm) => {
                    const term = String(searchTerm || '').trim();
                    const clean = value => String(value ?? '').replace(/\s+/g, ' ').trim();
                    const isDocTable = table => {
                        const text = clean(table.innerText || table.textContent || '');
                        return /Reference\s+File|Application\s+Form|Supporting\s+Documentation|downloadFile/i.test(text + ' ' + (table.innerHTML || ''));
                    };
                    const tables = Array.from(document.querySelectorAll('table')).filter(isDocTable);
                    if (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable) {
                        for (const table of tables) {
                            try {
                                if (window.jQuery.fn.dataTable.isDataTable && !window.jQuery.fn.dataTable.isDataTable(table)) continue;
                                const dt = window.jQuery(table).DataTable();
                                if (dt.search) dt.search(term);
                                if (dt.page && dt.page.len) dt.page.len(-1).draw(false);
                                else if (dt.draw) dt.draw(false);
                            } catch (e) {}
                        }
                    }
                    const fire = (el, type) => { try { el.dispatchEvent(new Event(type, {bubbles: true})); } catch (e) {} };
                    for (const table of tables) {
                        const id = table.id || '';
                        if (!id) continue;
                        for (const input of document.querySelectorAll(`input[type="search"][aria-controls="${CSS.escape(id)}"]`)) {
                            input.value = term; fire(input, 'input'); fire(input, 'keyup'); fire(input, 'change');
                        }
                        for (const sel of document.querySelectorAll(`select[aria-controls="${CSS.escape(id)}"]`)) {
                            const opts = Array.from(sel.options || []);
                            const chosen = opts.find(o => o.value === '-1') || opts.find(o => o.value === '100') || opts[opts.length - 1];
                            if (chosen) { sel.value = chosen.value; fire(sel, 'change'); }
                        }
                    }
                }
                """,
                search_term,
            )
        except Exception:
            return
        try:
            scope.wait_for_timeout(250)
        except Exception:
            pass



    def _document_names_visible_on_page(self, page: Any) -> set[str]:
        names: set[str] = set()
        for scope in self._document_scopes(page):
            try:
                texts = scope.evaluate(
                    r"""
                    () => {
                        const roots = [];
                        const seenRoots = new Set();
                        const addRoot = root => {
                            if (!root || seenRoots.has(root)) return;
                            seenRoots.add(root);
                            roots.push(root);
                            let nodes = [];
                            try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                            for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
                        };
                        addRoot(document);
                        const out = [];
                        const seen = new Set();
                        for (const root of roots) {
                            let nodes = [];
                            try { nodes = Array.from(root.querySelectorAll('table tbody tr, table tr, [role="row"], li')); } catch (e) { nodes = []; }
                            for (const el of nodes) {
                                if (seen.has(el)) continue;
                                seen.add(el);
                                const text = el.innerText || el.textContent || '';
                                if (text) out.push(text);
                            }
                        }
                        return out.slice(0, 1000);
                    }
                    """
                )
            except Exception:
                texts = []
            for text in texts or []:
                name = self._document_name_for_text(text)
                if name:
                    names.add(name)
        return names



    def _extract_application_form_text_from_page(self, page: Any, base_url: str) -> str:
        """Download the Application Form once, without scanning hundreds of rows."""
        timeout_ms = int(self.config.get("application_form_download_timeout_ms", 12000) or 12000)
        for scope in self._document_scopes(page):
            try:
                self._set_document_table_to_all_rows(scope, "Application Form")
            except Exception:
                pass
        self._force_supporting_docs_visible(page)
        return self._extract_application_form_text_by_exact_js_click(page, base_url, timeout_ms)

    def _extract_application_form_text_by_exact_js_click(self, page: Any, base_url: str, timeout_ms: int) -> str:
        """Find the exact Application Form row and capture its PDF once.

        Toronto has used both attachment downloads and PDF-viewer/new-tab
        behavior.  Event listeners cover both without stacking mutually
        exclusive ``expect_*`` waits.  The poll has one hard deadline.
        """
        find_js = r"""
        () => {
            const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const roots = [];
            const seen = new Set();
            const addRoot = root => {
                if (!root || seen.has(root)) return;
                seen.add(root); roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);
            for (const root of roots) {
                let rows = [];
                try { rows = Array.from(root.querySelectorAll('table tbody tr, table tr, tr, [role="row"]')); } catch (e) { rows = []; }
                for (const tr of rows.slice(0, 400)) {
                    const cells = Array.from(tr.querySelectorAll(':scope > td, :scope > [role="cell"]'));
                    const first = normalize(cells[0]?.innerText || cells[0]?.textContent || tr.innerText || tr.textContent || '');
                    if (!/\bApplication\s+Form\b/i.test(first)) continue;
                    let button = tr.querySelector('button.downloadFile[data-id], .downloadFile[data-id]');
                    if (!button) button = Array.from(tr.querySelectorAll('button, a, [role="button"]')).find(el => /Download|Open/i.test(normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || '')));
                    if (button) return {found: true, dataId: button.getAttribute('data-id') || ''};
                }
            }
            return {found: false};
        }
        """
        click_js = r"""
        () => {
            const normalize = value => String(value ?? '').replace(/\s+/g, ' ').trim();
            const roots = [];
            const seen = new Set();
            const addRoot = root => {
                if (!root || seen.has(root)) return;
                seen.add(root); roots.push(root);
                let nodes = [];
                try { nodes = Array.from(root.querySelectorAll('*')); } catch (e) { nodes = []; }
                for (const node of nodes) if (node.shadowRoot) addRoot(node.shadowRoot);
            };
            addRoot(document);
            for (const root of roots) {
                let rows = [];
                try { rows = Array.from(root.querySelectorAll('table tbody tr, table tr, tr, [role="row"]')); } catch (e) { rows = []; }
                for (const tr of rows.slice(0, 400)) {
                    const cells = Array.from(tr.querySelectorAll(':scope > td, :scope > [role="cell"]'));
                    const first = normalize(cells[0]?.innerText || cells[0]?.textContent || tr.innerText || tr.textContent || '');
                    if (!/\bApplication\s+Form\b/i.test(first)) continue;
                    let button = tr.querySelector('button.downloadFile[data-id], .downloadFile[data-id]');
                    if (!button) button = Array.from(tr.querySelectorAll('button, a, [role="button"]')).find(el => /Download|Open/i.test(normalize(el.innerText || el.textContent || el.getAttribute('aria-label') || '')));
                    if (!button) continue;
                    try { button.scrollIntoView({block: 'center'}); } catch (e) {}
                    button.click();
                    return {clicked: true, dataId: button.getAttribute('data-id') || ''};
                }
            }
            return {clicked: false};
        }
        """

        for scope in self._document_scopes(page):
            try:
                probe = scope.evaluate(find_js) or {}
            except Exception:
                continue
            if not probe.get("found"):
                continue

            downloads: list[Any] = []
            popups: list[Any] = []
            response_urls: list[str] = []

            def on_download(download: Any) -> None:
                downloads.append(download)

            def on_popup(popup: Any) -> None:
                popups.append(popup)

            def on_response(response: Any) -> None:
                try:
                    headers = {k.lower(): v for k, v in (response.headers or {}).items()}
                    if self._response_looks_like_download(response.url, headers):
                        response_urls.append(normalize_key(response.url))
                except Exception:
                    return

            try:
                page.on("download", on_download)
                page.on("popup", on_popup)
                page.on("response", on_response)
                original_url = normalize_key(page.url)
                result = scope.evaluate(click_js)
                LOGGER.info("Application Form exact click result for %s: %s", base_url, result)
                if not (result or {}).get("clicked"):
                    continue

                deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
                while time.monotonic() < deadline:
                    current_url = normalize_key(page.url)
                    if downloads or popups or response_urls or (current_url and current_url != original_url):
                        try:
                            page.wait_for_timeout(500)
                        except Exception:
                            pass
                        break
                    try:
                        page.wait_for_timeout(200)
                    except Exception:
                        break

                file_bytes = b""
                filename = ""
                if downloads:
                    download = downloads[0]
                    file_bytes = self._read_playwright_download_bytes(download)
                    filename = normalize_key(getattr(download, "suggested_filename", ""))
                    try:
                        download.delete()
                    except Exception:
                        pass

                # A PDF viewer or new tab may be used instead of a download.
                if not file_bytes:
                    candidate_pages = list(popups)
                    if normalize_key(page.url) != original_url:
                        candidate_pages.append(page)
                    for candidate_page in candidate_pages[:2]:
                        try:
                            candidate_url = normalize_key(candidate_page.url)
                            file_bytes = self._read_browser_url_bytes(candidate_page, candidate_url, timeout_ms)
                            if file_bytes:
                                filename = urlparse(candidate_url).path.rsplit("/", 1)[-1]
                                break
                        except Exception as exc:
                            LOGGER.info("Could not read Application Form PDF viewer for %s: %s", base_url, exc)

                # Last resort: replay a captured GET file endpoint through the
                # browser context's request client, which shares session cookies.
                if not file_bytes:
                    for candidate_url in reversed(response_urls[-5:]):
                        file_bytes = self._read_browser_url_bytes(page, candidate_url, timeout_ms)
                        if file_bytes:
                            filename = urlparse(candidate_url).path.rsplit("/", 1)[-1]
                            break

                text = self._extract_application_form_text_from_bytes(file_bytes, filename)
                if normalize_key(text):
                    LOGGER.info(
                        "Captured Toronto Application Form for %s; filename=%s; extracted_chars=%s",
                        base_url,
                        filename or "unknown",
                        len(text),
                    )
                    return text
            except Exception as exc:
                LOGGER.info("Application Form capture failed for %s: %s", base_url, exc)
            finally:
                try:
                    page.remove_listener("download", on_download)
                    page.remove_listener("popup", on_popup)
                    page.remove_listener("response", on_response)
                except Exception:
                    pass
                for popup in popups:
                    try:
                        popup.close()
                    except Exception:
                        pass
            break

        LOGGER.info("Application Form row/download not available for %s", base_url)
        return ""

    def _read_browser_url_bytes(self, page: Any, url: str, timeout_ms: int) -> bytes:
        """Read an HTTP(S), data, or blob URL with a hard browser-side bound."""
        url = normalize_key(url)
        if not url:
            return b""
        if url.startswith("data:"):
            try:
                import base64
                header, payload = url.split(",", 1)
                return base64.b64decode(payload) if ";base64" in header.lower() else unquote_plus(payload).encode("utf-8")
            except Exception:
                return b""
        if url.startswith("blob:"):
            try:
                encoded = page.evaluate(
                    r"""
                    async ({url, timeoutMs}) => {
                        const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('blob timeout')), timeoutMs));
                        const work = fetch(url).then(r => r.arrayBuffer()).then(buffer => {
                            const bytes = new Uint8Array(buffer);
                            let binary = '';
                            const chunk = 0x8000;
                            for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
                            return btoa(binary);
                        });
                        return await Promise.race([work, timeout]);
                    }
                    """,
                    {"url": url, "timeoutMs": max(1000, timeout_ms)},
                )
                if encoded:
                    import base64
                    return base64.b64decode(encoded)
            except Exception:
                return b""
        if url.startswith(("http://", "https://")):
            try:
                response = page.context.request.get(url, timeout=max(1000, timeout_ms), fail_on_status_code=False)
                if response.ok:
                    return response.body()
            except Exception:
                return b""
        return b""

    def _read_playwright_download_bytes(self, download: Any) -> bytes:
        try:
            path = download.path()
            if path and os.path.exists(path):
                with open(path, "rb") as fh:
                    return fh.read()
        except Exception:
            pass

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            download.save_as(tmp_path)
            with open(tmp_path, "rb") as fh:
                return fh.read()
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        return b""

    def _extract_application_form_text_from_bytes(self, file_bytes: bytes, filename: str = "") -> str:
        if not file_bytes:
            return ""
        lowered_name = normalize_key(filename).lower()
        if file_bytes[:5] == b"%PDF-" or lowered_name.endswith(".pdf"):
            return self._extract_pdf_text(file_bytes)

        try:
            text = file_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = file_bytes.decode("latin-1", errors="replace")

        if "<" in text[:5000] and re.search(r"<html|<body|<div|<span|<table", text[:5000], flags=re.I):
            return BeautifulSoup(text, "html.parser").get_text("\n", strip=True)
        return text

    def _looks_expired(self, html_text: str) -> bool:
        page_text = BeautifulSoup(html_text or "", "html.parser").get_text(" ", strip=True).lower()
        markers = (
            "page not found", "404 not found", "error 404", "access denied",
            "you are not authorized", "request forbidden",
        )
        return any(marker in page_text for marker in markers)

    def _extract_document_links(self, html_text: str, base_url: str) -> dict[str, str]:
        """Extract safe direct links or explicit application-page availability."""
        soup = BeautifulSoup(html_text or "", "html.parser")
        docs = {name: "" for name in self.REQUIRED_DOCUMENTS}

        def document_name(text: Any) -> str:
            return self._document_name_for_text(normalize_key(text))

        # Synthetic rows produced by _download_links_fixture_html are trusted
        # because they distinguish a real direct URL from row availability.
        for row in soup.select("tr[data-captured-document]"):
            name = document_name(row.get_text(" ", strip=True))
            link = row.find("a", href=True)
            if not name or not link:
                continue
            href = urljoin(base_url, normalize_key(link.get("href")))
            kind = normalize_key(row.get("data-captured-document")).lower()
            if kind == "available-on-page":
                parsed = urlparse(href)
                if "toronto.ca" in parsed.netloc.lower() and "/application-details/" in parsed.path.lower():
                    docs[name] = self._supporting_docs_anchor(href)
            elif self._is_meaningful_document_url(href, base_url):
                docs[name] = href

        def context_text(element: Any) -> str:
            pieces = [element.get_text(" ", strip=True)]
            parent = element.find_parent(["tr", "li"])
            if parent:
                pieces.append(parent.get_text(" ", strip=True))
            return normalize_key(" ".join(piece for piece in pieces if piece))

        def candidates(element: Any) -> list[str]:
            values: list[str] = []
            nodes = [element]
            try:
                nodes.extend(element.find_all(True))
            except Exception:
                pass
            for node in nodes:
                for attr_name, attr_value in getattr(node, "attrs", {}).items():
                    attr_lower = str(attr_name).lower()
                    if attr_lower == "data-id":
                        continue
                    if attr_lower in {
                        "href", "src", "formaction", "data-href", "data-url",
                        "data-download-url", "data-document-url", "data-file-url",
                    }:
                        if isinstance(attr_value, list):
                            values.extend(str(value) for value in attr_value)
                        else:
                            values.append(str(attr_value))
            seen: set[str] = set()
            output: list[str] = []
            for value in values:
                value = normalize_key(html.unescape(value))
                if value and value not in seen:
                    seen.add(value)
                    output.append(value)
            return output

        # Normal rendered rows/links may only contribute genuine file endpoints.
        for element in soup.select(
            "table tbody tr, table tr, a[href], button, [formaction], "
            "[data-href], [data-url], [data-download-url], [data-document-url], [data-file-url]"
        ):
            name = document_name(context_text(element))
            if not name or docs.get(name):
                continue
            for candidate in candidates(element):
                full_url = urljoin(base_url, candidate)
                if self._is_meaningful_document_url(full_url, base_url):
                    docs[name] = full_url
                    break

        return docs

    def _empty_party_contacts(self) -> dict[str, dict[str, str]]:
        return {
            "land_owner": {"name": "", "phone": "", "email": "", "address": ""},
            "applicant": {"name": "", "phone": "", "email": "", "address": ""},
        }

    def _contacts_have_content(self, contacts: dict[str, dict[str, str]] | None) -> bool:
        if not isinstance(contacts, dict):
            return False
        for party in ("land_owner", "applicant"):
            values = contacts.get(party) if isinstance(contacts.get(party), dict) else {}
            if any(normalize_key(value) for value in values.values()):
                return True
        return False

    def _extract_application_form_contacts_from_rendered_html(self, html_text: str) -> dict[str, dict[str, str]]:
        contacts = self._empty_party_contacts()
        soup = BeautifulSoup(html_text or "", "html.parser")
        node = soup.find(id="captured-toronto-application-form-text")
        if not node:
            return contacts
        text = node.get_text("\n", strip=True)
        if not normalize_key(text):
            return contacts
        parsed = self._parse_application_form_contacts(text)
        return parsed if self._contacts_have_content(parsed) else contacts

    def _extract_application_form_contacts(self, application_form_url: str | None) -> dict[str, dict[str, str]]:
        contacts = self._empty_party_contacts()
        if not application_form_url:
            return contacts

        parsed = urlparse(application_form_url)
        if "/application-details/" in parsed.path and ("supporting-documentation" in parsed.fragment.lower() or not re.search(r"\.(?:pdf)(?:$|\?)", application_form_url, re.I)):
            # Page anchors and data-id fragments are navigation aids, not files.
            return contacts

        try:
            response = self.http.get(application_form_url)
        except Exception as exc:
            LOGGER.info("Could not download Toronto application form %s: %s", application_form_url, exc)
            return contacts

        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or response.content[:5] == b"%PDF-" or application_form_url.lower().split("?")[0].endswith(".pdf"):
            text = self._extract_pdf_text(response.content)
        else:
            text = BeautifulSoup(response.text or "", "html.parser").get_text("\n", strip=True)

        return self._parse_application_form_contacts(text)

    def _extract_pdf_text(self, pdf_bytes: bytes) -> str:
        """Extract page-1 owner/applicant data using text, geometry and widgets.

        Toronto's form places the owner and applicant blocks in the lower part
        of page 1, but PDF reading order is often poor.  This method keeps the
        lower-page extraction requested by the monitor, while also creating
        label/value pairs from field geometry and AcroForm widgets before OCR.
        """
        text_parts: list[str] = []
        doc = None
        try:
            import fitz

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            if len(doc) < 1:
                return ""
            page = doc[0]
            page_rect = page.rect
            ratio = float(self.config.get("application_form_lower_start_ratio", 0.42) or 0.42)
            ratio = min(0.72, max(0.28, ratio))
            lower = fitz.Rect(
                page_rect.x0,
                page_rect.y0 + page_rect.height * ratio,
                page_rect.x1,
                page_rect.y1,
            )

            full_text = page.get_text("text", sort=True) or ""
            lower_text = page.get_text("text", clip=lower, sort=True) or ""
            if normalize_key(lower_text):
                text_parts.append(lower_text)
            # Keep the complete first page as a secondary source because some
            # form revisions move the owner heading slightly above the cutoff.
            if normalize_key(full_text) and normalize_key(full_text) != normalize_key(lower_text):
                text_parts.append("FULL PAGE 1\n" + full_text)

            def first_label_y(patterns: tuple[str, ...], minimum_y: float = 0.0) -> float | None:
                hits: list[float] = []
                for pattern in patterns:
                    try:
                        for rect in page.search_for(pattern):
                            if rect.y0 >= minimum_y:
                                hits.append(float(rect.y0))
                    except Exception:
                        continue
                return min(hits) if hits else None

            owner_y = first_label_y(("Registered Owner", "Property Owner", "Owner(s)"), lower.y0 - 40)
            applicant_y = first_label_y(("Applicant Name", "Applicant Information", "Applicant/Agent"), lower.y0 - 40)
            owner_start = owner_y if owner_y is not None else lower.y0
            owner_end = applicant_y if applicant_y is not None and applicant_y > owner_start else page_rect.y1
            applicant_start = applicant_y if applicant_y is not None else lower.y0
            applicant_end = page_rect.y1

            def field_value_near_label(label: str, y_start: float, y_end: float) -> str:
                try:
                    rects = [rect for rect in page.search_for(label) if rect.y0 >= y_start - 3 and rect.y1 <= y_end + 3]
                except Exception:
                    rects = []
                for rect in rects[:5]:
                    same_line = fitz.Rect(
                        min(page_rect.x1, rect.x1 + 3),
                        max(page_rect.y0, rect.y0 - 2),
                        page_rect.x1,
                        min(page_rect.y1, rect.y1 + 5),
                    )
                    value = normalize_key(page.get_text("text", clip=same_line, sort=True))
                    value = re.sub(r"^(?:[:\-]|Name|Address|Telephone|Phone|E-?mail)\s*", "", value, flags=re.I)
                    if value and compact_key(value) != compact_key(label):
                        return value
                    next_line = fitz.Rect(
                        page_rect.x0,
                        min(page_rect.y1, rect.y1 + 1),
                        page_rect.x1,
                        min(page_rect.y1, rect.y1 + 34),
                    )
                    value = normalize_key(page.get_text("text", clip=next_line, sort=True))
                    if value:
                        return value
                return ""

            geometry_specs = [
                ("owner_name", ("Registered Owner(s)", "Registered Owner", "Property Owner", "Owner Name"), owner_start, owner_end),
                ("owner_address", ("Business Address", "Mailing Address", "Owner Address"), owner_start, owner_end),
                ("owner_phone", ("Business Telephone", "Telephone", "Phone"), owner_start, owner_end),
                ("owner_email", ("Owner E-mail", "Owner Email", "Business E-mail", "Email"), owner_start, owner_end),
                ("applicant_name", ("Applicant Name", "Name of Applicant", "Applicant/Agent"), applicant_start, applicant_end),
                ("applicant_address", ("Business Address", "Mailing Address", "Applicant Address"), applicant_start, applicant_end),
                ("applicant_phone", ("Business Telephone", "Telephone", "Phone"), applicant_start, applicant_end),
                ("applicant_email", ("Business E-mail", "Applicant E-mail", "Applicant Email", "Email"), applicant_start, applicant_end),
            ]
            for key, labels, y0, y1 in geometry_specs:
                value = ""
                for label in labels:
                    value = field_value_near_label(label, y0, y1)
                    if value:
                        break
                if value:
                    text_parts.append(f"{key}: {value}")

            # AcroForm values are often the cleanest source.  Include all page-1
            # widgets with a hard cap; field names supply party/field context.
            try:
                widget = page.first_widget
                seen_widgets = 0
                while widget is not None and seen_widgets < 300:
                    seen_widgets += 1
                    field_name = normalize_key(getattr(widget, "field_name", ""))
                    field_value = normalize_key(getattr(widget, "field_value", ""))
                    rect = getattr(widget, "rect", None)
                    include = True
                    if rect is not None:
                        try:
                            include = fitz.Rect(rect).y0 >= lower.y0 - 60
                        except Exception:
                            include = True
                    if include and (field_name or field_value):
                        text_parts.append(f"{field_name}: {field_value}".strip(" :"))
                    try:
                        widget = widget.next
                    except Exception:
                        break
            except Exception as exc:
                LOGGER.info("Application Form widget extraction skipped: %s", exc)

            joined = "\n".join(part for part in text_parts if normalize_key(part))
            parsed_contacts = self._parse_application_form_contacts(joined)
            needs_ocr = len(joined.strip()) < 180 or not self._contacts_have_content(parsed_contacts)
            if needs_ocr and self.config.get("ocr_image_pdfs", True):
                try:
                    import pytesseract
                    from PIL import Image

                    pix = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5), alpha=False, clip=lower)
                    image = Image.open(io.BytesIO(pix.tobytes("png")))
                    timeout = int(self.config.get("ocr_timeout_seconds", 20) or 20)
                    ocr_text = pytesseract.image_to_string(image, timeout=timeout)
                    if normalize_key(ocr_text):
                        text_parts.append("OCR LOWER PAGE 1\n" + ocr_text)
                except Exception as exc:
                    LOGGER.info("OCR fallback unavailable/timed out for application form PDF: %s", exc)
        except Exception as exc:
            LOGGER.info("Could not parse application form PDF page 1: %s", exc)
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

        return "\n".join(part for part in text_parts if normalize_key(part))

    def _parse_application_form_contacts(self, text: str) -> dict[str, dict[str, str]]:
        """Parse owner/applicant details from visible text and PDF widget fields."""
        raw = (text or "").replace("\x00", " ")
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        clean = raw.strip()
        empty = self._empty_party_contacts()
        if not clean:
            return empty

        lines = [normalize_key(line) for line in clean.splitlines() if normalize_key(line)]
        field_pairs: list[tuple[str, str]] = []
        for line in lines:
            match = re.match(r"^([^:]{2,100})\s*:\s*(.*)$", line)
            if match:
                key = compact_key(match.group(1))
                value = normalize_key(match.group(2))
                if key and value:
                    field_pairs.append((key, value))

        def field_value(party_tokens: tuple[str, ...], value_tokens: tuple[str, ...]) -> str:
            candidates: list[str] = []
            for key, value in field_pairs:
                if not any(token in key for token in party_tokens):
                    continue
                if any(token in key for token in value_tokens):
                    candidates.append(value)
            return candidates[0] if candidates else ""

        def section(start_patterns: tuple[str, ...], end_patterns: tuple[str, ...]) -> str:
            starts = [m for pattern in start_patterns for m in [re.search(pattern, clean, re.I)] if m]
            if not starts:
                return ""
            start_match = min(starts, key=lambda m: m.start())
            remainder = clean[start_match.start():]
            ends = [m for pattern in end_patterns for m in [re.search(pattern, remainder[start_match.end()-start_match.start():], re.I)] if m]
            if not ends:
                return remainder
            end_match = min(ends, key=lambda m: m.start())
            offset = start_match.end() - start_match.start()
            return remainder[:offset + end_match.start()]

        owner_section = section(
            (r"Registered\s+Owner", r"Owner\(s\)\s+of\s+subject\s+land", r"Property\s+Owner"),
            (r"Applicant\s+Name", r"Applicant\s+Information", r"This\s+section\s+for\s+Office\s+Use", r"Agent\s+Information"),
        )
        applicant_section = section(
            (r"Applicant\s+Name", r"Applicant\s+Information"),
            (r"This\s+section\s+for\s+Office\s+Use", r"Declaration", r"File\s+No\.?", r"Owner'?s\s+Authorization"),
        )

        email_re = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
        phone_re = re.compile(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?:\s*(?:x|ext\.?)[-\s]*\d+)?", re.I)

        def first_email(segment: str) -> str:
            match = email_re.search(segment or "")
            return match.group(0) if match else ""

        def first_phone(segment: str) -> str:
            match = phone_re.search(segment or "")
            return normalize_key(match.group(0)) if match else ""

        def labelled_value(segment: str, labels: tuple[str, ...]) -> str:
            for label in labels:
                match = re.search(rf"{label}\s*:?\s*([^\n|]{{2,180}})", segment or "", re.I)
                if match:
                    value = normalize_key(match.group(1))
                    value = re.split(r"\s+(?:Owner\s+E-?mail|Business\s+(?:E-?mail|Telephone|Fax|Address)|Applicant\s+is)\b", value, maxsplit=1, flags=re.I)[0]
                    if value:
                        return value.strip(" :;,-")
            return ""

        def fallback_name(segment: str, forbidden: tuple[str, ...]) -> str:
            for line in [normalize_key(x) for x in (segment or "").splitlines() if normalize_key(x)]:
                lower = line.lower()
                if any(token in lower for token in forbidden):
                    continue
                if email_re.search(line) or phone_re.search(line):
                    continue
                if re.search(r"[A-Za-z]", line) and len(line) <= 160:
                    return line.strip(" :;,-")
            return ""

        def extract_address(segment: str) -> str:
            value = labelled_value(segment, (r"Business\s+Address", r"Mailing\s+Address", r"Address"))
            if value:
                return shorten(value, 300)
            seg_lines = [normalize_key(x) for x in (segment or "").splitlines() if normalize_key(x)]
            for index, line in enumerate(seg_lines):
                if re.search(r"business\s+address|mailing\s+address", line, re.I):
                    parts = []
                    for candidate in seg_lines[index + 1:index + 4]:
                        if re.search(r"telephone|fax|e-?mail|applicant\s+name", candidate, re.I):
                            break
                        parts.append(candidate)
                    if parts:
                        return shorten(", ".join(parts), 300)
            return ""

        owner = {
            "name": field_value(("owner", "registeredowner", "propertyowner"), ("name", "company", "corporation")),
            "phone": field_value(("owner", "registeredowner", "propertyowner"), ("phone", "telephone", "tel")),
            "email": field_value(("owner", "registeredowner", "propertyowner"), ("email", "mail")),
            "address": field_value(("owner", "registeredowner", "propertyowner"), ("address", "street")),
        }
        applicant = {
            "name": field_value(("applicant",), ("name", "company", "corporation")),
            "phone": field_value(("applicant",), ("phone", "telephone", "tel")),
            "email": field_value(("applicant",), ("email", "mail")),
            "address": field_value(("applicant",), ("address", "street")),
        }

        owner["email"] = owner["email"] or first_email(owner_section)
        owner["phone"] = owner["phone"] or first_phone(owner_section)
        owner["name"] = owner["name"] or labelled_value(owner_section, (r"Registered\s+Owner\(s\)", r"Registered\s+Owner", r"Owner\s+Name", r"Property\s+Owner"))
        owner["name"] = owner["name"] or fallback_name(owner_section, ("registered owner", "owner e-mail", "business address", "business telephone", "business fax"))
        owner["address"] = owner["address"] or extract_address(owner_section)

        applicant["email"] = applicant["email"] or first_email(applicant_section)
        applicant["phone"] = applicant["phone"] or first_phone(applicant_section)
        applicant["name"] = applicant["name"] or labelled_value(applicant_section, (r"Applicant\s+Name", r"Name\s+of\s+Applicant"))
        applicant["name"] = applicant["name"] or fallback_name(applicant_section, ("applicant name", "applicant information", "business e-mail", "business address", "business telephone", "applicant is"))
        applicant["address"] = applicant["address"] or extract_address(applicant_section)

        # Validate common contact fields and strip obvious labels accidentally captured.
        for party in (owner, applicant):
            if party["email"] and not email_re.fullmatch(party["email"]):
                found = first_email(party["email"])
                party["email"] = found
            if party["phone"]:
                found = first_phone(party["phone"])
                party["phone"] = found
            party["name"] = re.sub(r"^(?:Registered\s+Owner\(s\)|Registered\s+Owner|Applicant\s+Name)\s*:?\s*", "", party["name"], flags=re.I).strip(" :;,-")
            party["address"] = shorten(party["address"], 300)

        return {"land_owner": owner, "applicant": applicant}

    def _extract_label_value(self, text: str, labels: list[str]) -> str:
        if not text:
            return ""
        label_pattern = "|".join(re.escape(label) for label in labels)
        match = re.search(rf"(?:{label_pattern})\s*:?\s*(.+?)(?=\s+[A-Z][A-Za-z /()#.-]{{2,}}\s*:?|$)", text, flags=re.I)
        return normalize_key(match.group(1)) if match else ""

    def enrich_application(self, item: dict[str, Any]) -> dict[str, Any]:
        item = dict(item)
        raw_url = item.get("raw_application_url") or item.get("detail_url")
        detail_url = self._application_details_url(raw_url)
        item["detail_url"] = detail_url
        item["link_status"] = self._link_status(raw_url, detail_url)
        item["enrichment_status"] = "not_attempted"
        item.setdefault("document_links", {name: "" for name in self.REQUIRED_DOCUMENTS})
        item.setdefault("land_owner", {"name": "", "phone": "", "email": "", "address": ""})
        item.setdefault("applicant", {"name": "", "phone": "", "email": "", "address": ""})

        LOGGER.info(
            "Toronto enrich start: %s (%s)",
            item.get("file_number") or item.get("address") or "unknown application",
            detail_url or raw_url or "no-url",
        )

        if item["link_status"] != "current" or not detail_url:
            item["enrichment_status"] = "skipped"
            LOGGER.info("Toronto enrich skipped: link_status=%s", item["link_status"])
            return item

        try:
            LOGGER.info("Toronto render start: %s", detail_url)
            final_url, html_text = self._fetch_rendered_page(detail_url)
            LOGGER.info("Toronto render complete: %s bytes=%d", final_url or detail_url, len(html_text or ""))
            if self._looks_expired(html_text):
                item["link_status"] = "expired"
                item["enrichment_status"] = "expired"
                LOGGER.info("Toronto render indicated expired page: %s", final_url or detail_url)
                return item

            item["detail_url"] = final_url or detail_url
            item["document_links"] = self._extract_document_links(html_text, item["detail_url"])
            item["document_link_kind"] = {
                name: (
                    "application-page"
                    if "/application-details/" in urlparse(normalize_key(value)).path.lower()
                    else "direct"
                )
                for name, value in item["document_links"].items()
                if normalize_key(value)
            }
            found_docs = sum(1 for value in item["document_links"].values() if normalize_key(value))
            LOGGER.info(
                "Toronto supporting docs extracted: %d/%d found for %s",
                found_docs,
                len(self.REQUIRED_DOCUMENTS),
                item["detail_url"],
            )

            form_contacts = self._extract_application_form_contacts_from_rendered_html(html_text)
            if self._contacts_have_content(form_contacts):
                item.update(form_contacts)
                LOGGER.info("Toronto contact extraction succeeded from rendered Application Form for %s", item["detail_url"])
            else:
                form_url = item["document_links"].get("Application Form")
                fallback_contacts = (
                    self._extract_application_form_contacts(form_url)
                    if form_url and self._is_meaningful_document_url(form_url, item["detail_url"])
                    else self._empty_party_contacts()
                )
                if self._contacts_have_content(fallback_contacts):
                    item.update(fallback_contacts)
                    LOGGER.info("Toronto contact extraction succeeded from direct Application Form URL for %s", item["detail_url"])
                else:
                    LOGGER.info("Toronto contact extraction found no owner/applicant data for %s", item["detail_url"])
            status_codes = list(dict.fromkeys(re.findall(
                r'<div id="toronto-enrichment-status" data-status="([^"]+)"',
                html_text or "",
                flags=re.I,
            )))
            if status_codes:
                messages = {
                    "widget-not-ready": "City application widget did not paint.",
                    "supporting-section-not-detected": "Supporting Documentation could not be confirmed.",
                    "application-form-contacts-not-extracted": "Application Form was detected, but owner/applicant fields were not extracted.",
                }
                item["enrichment_status"] = "partial"
                item["enrichment_error"] = " ".join(messages.get(code, code) for code in status_codes)
            else:
                item["enrichment_status"] = "ok"
        except Exception as exc:
            item["enrichment_status"] = "failed"
            item["enrichment_error"] = shorten(str(exc), 500)
            LOGGER.exception("Could not enrich Toronto application page %s: %s", detail_url, exc)

        return item

class OttawaDevAppsMonitor:
    """Experimental monitor for City of Ottawa DevApps export data.

    The public DevApps interface is JavaScript-rendered. This class only works if
    `export_url` points to a stable machine-readable JSON/CSV export.
    """

    FIELD_ALIASES: dict[str, list[str]] = {
        "application_number": ["Application Number", "Application #", "applicationNumber", "application_number"],
        "application_date": ["Application Date", "Date Received", "dateReceived", "applicationDate"],
        "application_type": ["Application Type", "Application", "applicationType"],
        "address_number": ["Address Number", "Street Number", "addressNumber"],
        "road_name": ["Road Name", "Street Name", "roadName"],
        "road_type": ["Road Type", "Street Type", "roadType"],
        "status": ["Application Status", "Status", "applicationStatus"],
        "review_status": ["Object Status Type", "Review Status", "reviewStatus", "objectStatusType"],
        "status_date": ["Object Status Date", "Status Date", "statusDate", "objectStatusDate"],
        "file_lead": ["File Lead", "File Lead Name", "Planner", "fileLead"],
        "description": ["Brief Description", "Description", "briefDescription"],
        "ward_number": ["Ward #", "Ward Number", "wardNumber"],
        "ward": ["Ward", "ward"],
    }

    def __init__(self, http: HttpClient, config: dict[str, Any]) -> None:
        self.http = http
        self.config = config
        self.export_url = config.get("export_url", "https://devapps-restapi.ottawa.ca/devapps/ExportData")
        self.detail_base_url = config.get("detail_base_url", "https://devapps.ottawa.ca/en/applications").rstrip("/")

    def fetch_new_candidates(self) -> list[dict[str, Any]]:
        records = self._fetch_export_records()
        allowed_types = {compact_key(x) for x in self.config.get("application_types", []) if x}
        lookback_days = int(self.config.get("lookback_days", 45))
        cutoff = utcnow() - timedelta(days=lookback_days)
        max_records = int(self.config.get("max_records", 2000))

        grouped: dict[str, dict[str, Any]] = {}
        for record in records[:max_records]:
            item = self._normalize_record(record)
            app_number = item.get("application_number")
            if not app_number:
                continue
            actual_type = compact_key(item.get("application_type"))
            if allowed_types and not any(
                allowed in actual_type or actual_type in allowed
                for allowed in allowed_types
            ):
                continue
            date_value = parse_dt(item.get("application_date") or item.get("status_date"))
            if date_value and date_value < cutoff:
                continue

            existing = grouped.get(app_number)
            if not existing:
                item["addresses"] = [item["address"]] if item.get("address") else []
                grouped[app_number] = item
                continue

            if item.get("address") and item["address"] not in existing.setdefault("addresses", []):
                existing["addresses"].append(item["address"])
            if not existing.get("description") and item.get("description"):
                existing["description"] = item["description"]
            if not existing.get("status") and item.get("status"):
                existing["status"] = item["status"]
            if not existing.get("review_status") and item.get("review_status"):
                existing["review_status"] = item["review_status"]

        out = list(grouped.values())
        out.sort(key=lambda x: parse_dt(x.get("application_date") or x.get("status_date")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

    def _fetch_export_records(self) -> list[dict[str, Any]]:
        response = self.http.get(self.export_url)
        content_type = response.headers.get("content-type", "").lower()
        text = response.text.lstrip("\ufeff")

        if "doesn't work properly without javascript enabled" in text.lower():
            raise RuntimeError(
                "Ottawa DevApps returned the JavaScript shell instead of data; "
                "the public pages are not directly scrapeable with requests."
            )

        if "json" in content_type or text[:1] in "[{":
            data = response.json()
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                for key in ("data", "records", "results", "items"):
                    value = data.get(key)
                    if isinstance(value, list):
                        return [x for x in value if isinstance(x, dict)]
            return []

        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
            rows = list(csv.DictReader(io.StringIO(text, newline=""), dialect=dialect))
            if rows and len(rows[0].keys()) > 1:
                return rows
        except csv.Error:
            pass

        # Fallback: Ottawa's export can contain awkward embedded line breaks.
        # Try common delimiters explicitly before giving up.
        for delimiter in ("\t", ",", ";", "|"):
            try:
                reader = csv.DictReader(
                    io.StringIO(text, newline=""),
                    delimiter=delimiter,
                    quoting=csv.QUOTE_MINIMAL,
                )
                rows = list(reader)
                if rows and len(rows[0].keys()) > 1:
                    return rows
            except csv.Error:
                continue

        raise RuntimeError(
            "Ottawa ExportData did not return JSON or delimited rows. The configured "
            "endpoint may be unavailable or not a stable public API."
        )

    def _normalize_record(self, record: dict[str, Any]) -> dict[str, Any]:
        keymap = {compact_key(k): k for k in record.keys()}

        def pick(logical: str) -> Any:
            for alias in self.FIELD_ALIASES[logical]:
                c = compact_key(alias)
                if c in keymap and record.get(keymap[c]) not in (None, ""):
                    return record[keymap[c]]
            for field_compact, original in keymap.items():
                if any(compact_key(alias) in field_compact for alias in self.FIELD_ALIASES[logical]):
                    if record.get(original) not in (None, ""):
                        return record[original]
            return None

        number = normalize_key(pick("address_number"))
        road_name = normalize_key(pick("road_name"))
        road_type = normalize_key(pick("road_type"))
        address = normalize_key(" ".join(x for x in [number, road_name, road_type] if x))
        app_number = normalize_key(pick("application_number"))
        detail_url = f"{self.detail_base_url}/{quote_plus(app_number)}/details" if app_number else None

        return {
            "application_number": app_number,
            "application_date": normalize_key(pick("application_date")),
            "application_type": normalize_key(pick("application_type")),
            "address": address,
            "status": normalize_key(pick("status")),
            "review_status": normalize_key(pick("review_status")),
            "status_date": normalize_key(pick("status_date")),
            "file_lead": normalize_key(pick("file_lead")),
            "description": shorten(normalize_key(pick("description")), 1200),
            "ward_number": normalize_key(pick("ward_number")),
            "ward": normalize_key(pick("ward")),
            "detail_url": detail_url,
            "raw": record,
        }

class NewsMonitor:
    def __init__(self, http: HttpClient, config: dict[str, Any]) -> None:
        self.http = http
        self.config = config

    def fetch_relevant_posts(self) -> list[dict[str, Any]]:
        posts: list[dict[str, Any]] = []
        for site in self.config.get("sites", []):
            site_posts = self._fetch_site_posts(site)
            for post in site_posts:
                published_dt = parse_dt(post.get("published"))
                lookback = utcnow() - timedelta(days=int(self.config.get("lookback_days", 21)))
                if published_dt and published_dt < lookback:
                    continue
                enriched = self._enrich_post(post)
                score, evidence = self._score_post(enriched)
                if score >= int(self.config.get("minimum_keyword_score", 2)):
                    enriched["score"] = score
                    enriched["evidence_keywords"] = evidence
                    posts.append(enriched)
        return posts

    def _fetch_site_posts(self, site: dict[str, Any]) -> list[dict[str, Any]]:
        max_posts = int(self.config.get("max_posts_per_site", 50))
        out: list[dict[str, Any]] = []
        for feed_url in site.get("feed_urls", []):
            try:
                parsed = feedparser.parse(feed_url)
                for entry in parsed.entries[:max_posts]:
                    out.append(
                        {
                            "source": site.get("name") or site.get("base_url"),
                            "title": shorten(getattr(entry, "title", ""), 300),
                            "url": getattr(entry, "link", None),
                            "published": getattr(entry, "published", None) or getattr(entry, "updated", None),
                            "summary": shorten(getattr(entry, "summary", ""), 1000),
                            "categories": [getattr(tag, "term", "") for tag in getattr(entry, "tags", [])],
                        }
                    )
            except Exception as exc:
                LOGGER.warning("Feed failed for %s: %s", feed_url, exc)
        if out:
            return self._dedupe_posts(out)[:max_posts]
        return self._scrape_homepage(site)[:max_posts]

    def _scrape_homepage(self, site: dict[str, Any]) -> list[dict[str, Any]]:
        base_url = site.get("base_url")
        if not base_url:
            return []
        response = self.http.get(base_url)
        soup = BeautifulSoup(response.text, "html.parser")
        posts: list[dict[str, Any]] = []
        for a in soup.find_all("a", href=True):
            title = shorten(a.get_text(" ", strip=True), 250)
            href = urljoin(base_url, a["href"])
            if not title or len(title) < 12:
                continue
            if not href.startswith(base_url.rstrip("/")):
                continue
            posts.append({"source": site.get("name") or base_url, "title": title, "url": href, "published": None, "summary": "", "categories": []})
        return self._dedupe_posts(posts)

    def _dedupe_posts(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for post in posts:
            key = post.get("url") or post.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(post)
        return unique

    def _enrich_post(self, post: dict[str, Any]) -> dict[str, Any]:
        url = post.get("url")
        article_text = ""
        if url:
            try:
                response = self.http.get(url)
                soup = BeautifulSoup(response.text, "html.parser")
                for bad in soup(["script", "style", "nav", "footer", "aside"]):
                    bad.decompose()
                article = soup.find("article") or soup.find("main") or soup.body or soup
                article_text = shorten(article.get_text(" ", strip=True), 6000)
            except Exception as exc:
                LOGGER.warning("Could not fetch news article %s: %s", url, exc)
        text = " ".join([str(post.get("title") or ""), str(post.get("summary") or ""), article_text])
        post = dict(post)
        post["article_text"] = article_text
        post["extracted"] = self._extract_news_fields(text)
        return post

    def _score_post(self, post: dict[str, Any]) -> tuple[int, list[str]]:
        text = " ".join(
            [
                str(post.get("title") or ""),
                str(post.get("summary") or ""),
                str(post.get("article_text") or ""),
                " ".join(post.get("categories") or []),
            ]
        ).lower()
        score = 0
        evidence: list[str] = []
        for keyword, weight in NEWS_KEYWORDS.items():
            if keyword in text:
                score += weight
                evidence.append(keyword)
        for keyword, weight in NEGATIVE_NEWS_KEYWORDS.items():
            if keyword in text:
                score += weight
        return score, evidence[:12]

    def _extract_news_fields(self, text: str) -> dict[str, Any]:
        clean = shorten(text, 6000)
        return {
            "location": self._extract_location(clean),
            "footprint": self._extract_footprint(clean),
            "building_type": self._infer_building_type(clean),
            "development_timelines": self._extract_timelines(clean),
            "influencing_parties": self._extract_parties(clean),
        }

    def _extract_location(self, text: str) -> str:
        patterns = [
            r"(?:in|at|near|for)\s+([A-Z][A-Za-z .'-]+,\s*(?:Ont\.?|Ontario|Que\.?|Quebec|B\.C\.|British Columbia|Alta\.?|Alberta|Manitoba|Saskatchewan|Nova Scotia|New Brunswick))",
            r"([0-9]{1,5}\s+[A-Z][A-Za-z0-9 .'-]+\s+(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Drive|Dr\.?|Boulevard|Blvd\.?|Way|Court|Lane)[^.,;]*)",
            r"\b(Toronto|GTA|Mississauga|Brampton|Vaughan|Markham|Oakville|Burlington|Hamilton|Ottawa|Calgary|Edmonton|Vancouver|Montreal|Winnipeg|Halifax)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return shorten(match.group(1), 200)
        return "Not found"

    def _extract_footprint(self, text: str) -> list[str]:
        patterns = [
            r"\b[0-9][0-9,\.]*\s*(?:million\s+)?(?:square\s+feet|sq\.?\s*ft\.?|sf|square\s+metres|sq\.?\s*m\.?|m2|acres|hectares)\b",
            r"\b[0-9][0-9,\.]*\s*(?:storey|storeys|stories|storeys?)\b",
            r"\b[0-9][0-9,\.]*\s*(?:units|suites|beds|parking spaces)\b",
        ]
        found: list[str] = []
        for pattern in patterns:
            found.extend(re.findall(pattern, text, flags=re.I))
        return list(dict.fromkeys(shorten(x, 120) for x in found))[:8]

    def _infer_building_type(self, text: str) -> str:
        candidates = [
            ("industrial / warehouse / logistics", r"industrial|warehouse|logistics|distribution"),
            ("data centre", r"data centre|data center"),
            ("multifamily / rental residential", r"multifamily|multi-family|rental apartment|purpose-built rental|PBR"),
            ("condominium / residential tower", r"condo|condominium|residential tower"),
            ("mixed-use", r"mixed[- ]use"),
            ("office", r"office"),
            ("retail", r"retail|shopping centre|shopping center"),
            ("manufacturing / food facility", r"manufacturing|plant|food processing|facility"),
        ]
        lower = text.lower()
        matches = [label for label, pattern in candidates if re.search(pattern, lower, re.I)]
        return "; ".join(matches[:3]) if matches else "Not found"

    def _extract_timelines(self, text: str) -> list[str]:
        patterns = [
            r"\b(?:Q[1-4]\s*)?20[2-9][0-9]\b",
            r"\b(?:spring|summer|fall|autumn|winter)\s+20[2-9][0-9]\b",
            r"\b(?:by|in|during|through|until|starting|beginning|complete(?:d|ion)?|open(?:ing)?|deliver(?:y|ed)?|occupancy)\s+[^.;]{0,80}\b(?:20[2-9][0-9]|Q[1-4])\b",
            r"\b(?:under construction|construction is underway|breaks ground|broke ground|site preparation|pre-construction|approved|rezoning|site plan approval)\b",
        ]
        found: list[str] = []
        for pattern in patterns:
            found.extend(re.findall(pattern, text, flags=re.I))
        return list(dict.fromkeys(shorten(x, 160) for x in found))[:10]

    def _extract_parties(self, text: str) -> list[str]:
        # Pull named organizations near common role terms, plus corporate suffixes.
        party_patterns = [
            r"\b([A-Z][A-Za-z&.' -]{2,80}\s+(?:Properties|Developments|Development|Group|Corp\.?|Corporation|Inc\.?|Ltd\.?|LP|REIT|Capital|Realty|Construction|Architects|Partners|Foods|Technologies))\b",
            r"(?:developer|owner|builder|architect|contractor|partner|tenant|investor|lender|municipality|agency)\s+(?:is|was|are|include[s]?|including|with|by|from)?\s*([A-Z][A-Za-z&.' -]{2,80})",
        ]
        found: list[str] = []
        for pattern in party_patterns:
            for match in re.findall(pattern, text):
                candidate = shorten(match, 100).strip(" -,.:")
                if len(candidate.split()) <= 10 and candidate not in found:
                    found.append(candidate)
        return found[:10]


class Notifier:
    def __init__(self, config: dict[str, Any], dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

    def send(self, items: list[NotificationItem]) -> None:
        if not items:
            LOGGER.info("No new matching development items.")
            return
        delivery_mode = str(self.config.get("notifications", {}).get("delivery_mode", "digest")).lower()
        LOGGER.info("Preparing notification(s) for %d new item(s): %s", len(items), summarize_sources(items))
        if delivery_mode == "individual":
            for item in items:
                self._send_items([item])
            return
        self._send_items(items)

    def _send_items(self, items: list[NotificationItem]) -> bool:
        subject = f"Development project monitor: {len(items)} new item(s)"
        if len(items) == 1:
            subject = f"Development project monitor: {items[0].title}"
        text_body = self._render_text(items)
        json_body = [dataclasses.asdict(item) for item in items]
        if self.dry_run:
            print(text_body)
            return True
        delivered = False
        if self._send_slack(text_body):
            delivered = True
        if self._send_generic_webhook(subject, json_body):
            delivered = True
        if self._send_email(subject, text_body):
            delivered = True
        if not delivered:
            LOGGER.warning("No notification channel was enabled or configured; printing notification to stdout.")
            print(text_body)
        return delivered

    def _render_text(self, items: list[NotificationItem]) -> str:
        sections = ["Development Project Monitor", f"Generated: {utcnow().isoformat()}", ""]
        for idx, item in enumerate(items, start=1):
            p = item.payload
            sections.append(f"{idx}. [{item.kind}] {item.title}")
            if item.url:
                sections.append(f"URL: {item.url}")
            if item.kind in {"toronto_open_data", "toronto_aic"}:
                sections.extend(self._render_toronto(p))
            elif item.kind == "ottawa_devapps":
                sections.extend(self._render_ottawa(p))
            else:
                sections.extend(self._render_news(p))
            sections.append("-" * 72)
        return "\n".join(sections)

    def _render_toronto(self, p: dict[str, Any]) -> list[str]:
        if bool(self.config.get("notifications", {}).get("simplified_report", False)):
            return self._render_toronto_simplified(p)

        meeting_bits = [
            p.get("community_meeting_date") or "",
            p.get("community_meeting_time") or "",
            p.get("community_meeting_location") or "",
        ]
        meeting = " | ".join(bit for bit in meeting_bits if bit) or "Not found"
        contact_bits = [p.get("contact_name") or "", p.get("contact_phone") or "", p.get("contact_email") or ""]
        contact = " | ".join(bit for bit in contact_bits if bit) or "Not found"
        ward = " ".join(bit for bit in [p.get("ward_number") or "", p.get("ward") or ""] if bit) or "Not found"
        csv_rows = ", ".join(p.get("csv_row_ids") or []) or "Not found"
        file_numbers = ", ".join(p.get("file_numbers") or []) or p.get("file_number") or "Not found"

        lines = [
            f"File number(s): {file_numbers}",
            f"Addresses: {self._format_addresses_with_map(p)}",
            f"Metadata rows: {p.get('metadata_row_count') or 1} ({csv_rows})",
            f"Type/status: {p.get('application_type') or 'Not found'} / {p.get('status') or 'Not found'}",
            f"Submitted: {p.get('submitted_date') or 'Not found'}",
            f"Ward: {ward}",
            f"Community meeting: {meeting}",
            f"Contact: {contact}",
            f"Link status: {p.get('link_status') or 'Not found'}",
            f"Enrichment: {p.get('enrichment_status') or 'Not attempted'}"
            + (f" | {p.get('enrichment_error')}" if p.get('enrichment_error') else ""),
            f"Description: {p.get('description') or 'Not found'}",
            f"Land Owner: {self._format_party(p.get('land_owner'))}",
            f"Applicant: {self._format_party(p.get('applicant'))}",
        ]
        lines.extend(self._format_document_links(p.get("document_links") or {}))
        return lines

    def _render_toronto_simplified(self, p: dict[str, Any]) -> list[str]:
        lines = [
            f"Addresses: {self._format_addresses_with_map(p)}",
            f"Enrichment: {p.get('enrichment_status') or 'Not attempted'}"
            + (f" | {p.get('enrichment_error')}" if p.get('enrichment_error') else ""),
            f"Description: {p.get('description') or 'Not found'}",
            f"Land Owner: {self._format_party(p.get('land_owner'))}",
            f"Applicant: {self._format_party(p.get('applicant'))}",
        ]
        lines.extend(self._format_document_links(p.get("document_links") or {}))
        return lines

    def _format_addresses_with_map(self, p: dict[str, Any]) -> str:
        addresses = p.get("addresses") or ([p.get("address")] if p.get("address") else [])
        addresses = [normalize_key(address) for address in addresses if normalize_key(address)]
        if not addresses:
            return "Not found"
        first_address = addresses[0]
        map_query = first_address
        if "toronto" not in map_query.lower():
            map_query = f"{map_query}, Toronto, ON"
        map_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(map_query)}"
        return f"{'; '.join(addresses)} | Google Maps: {map_url}"

    def _format_party(self, party: Any) -> str:
        if not isinstance(party, dict):
            return "Not found"
        bits = [
            party.get("name") or "",
            party.get("phone") or "",
            party.get("email") or "",
            party.get("address") or "",
        ]
        return " | ".join(bit for bit in bits if bit) or "Not found"

    def _format_document_links(self, document_links: dict[str, str]) -> list[str]:
        required = [
            "Application Form",
            "Architectural Plans",
            "Civil and Utilities Plans",
            "Geotechnical Study",
            "Hydrogeological Report",
        ]
        lines = ["Document links:"]
        for name in required:
            value = normalize_key((document_links or {}).get(name))
            if not value:
                lines.append(f"  - {name}: Not found")
                continue
            parsed = urlparse(value)
            if "/application-details/" in parsed.path.lower():
                page_url = value.split("#", 1)[0]
                lines.append(
                    f"  - {name}: Available — open Supporting Documentation: {page_url}"
                )
            else:
                lines.append(f"  - {name}: {value}")
        return lines

    def _render_ottawa(self, p: dict[str, Any]) -> list[str]:
        addresses = ", ".join(p.get("addresses") or []) or p.get("address") or "Not found"
        return [
            f"Application number: {p.get('application_number') or 'Not found'}",
            f"Address(es): {addresses}",
            f"Application type: {p.get('application_type') or 'Not found'}",
            f"Status/review: {p.get('status') or 'Not found'} / {p.get('review_status') or 'Not found'}",
            f"Date received: {p.get('application_date') or 'Not found'}",
            f"Status date: {p.get('status_date') or 'Not found'}",
            f"Ward: {p.get('ward_number') or ''} {p.get('ward') or 'Not found'}".strip(),
            f"File lead: {p.get('file_lead') or 'Not found'}",
            f"Description: {p.get('description') or 'Not found'}",
        ]

    def _render_news(self, p: dict[str, Any]) -> list[str]:
        extracted = p.get("extracted") or {}
        return [
            f"Source: {p.get('source')}",
            f"Published: {p.get('published') or 'Not found'}",
            f"Location: {extracted.get('location') or 'Not found'}",
            f"Footprint/size: {', '.join(extracted.get('footprint') or []) or 'Not found'}",
            f"Building type: {extracted.get('building_type') or 'Not found'}",
            f"Timelines: {', '.join(extracted.get('development_timelines') or []) or 'Not found'}",
            f"Influencing parties: {', '.join(extracted.get('influencing_parties') or []) or 'Not found'}",
            f"Evidence keywords: {', '.join(p.get('evidence_keywords') or [])}",
        ]

    def _send_slack(self, text_body: str) -> bool:
        cfg = self.config.get("notifications", {}).get("slack", {})
        if not cfg.get("enabled"):
            return False
        url = os.getenv(cfg.get("webhook_url_env", "SLACK_WEBHOOK_URL"), "")
        if not url:
            LOGGER.warning("Slack enabled but webhook env var is missing.")
            return False
        response = requests.post(url, json={"text": text_body[:35000]}, timeout=30)
        response.raise_for_status()
        return True

    def _send_generic_webhook(self, subject: str, items: list[dict[str, Any]]) -> bool:
        cfg = self.config.get("notifications", {}).get("generic_webhook", {})
        if not cfg.get("enabled"):
            return False
        url = os.getenv(cfg.get("url_env", "NOTIFY_WEBHOOK_URL"), "")
        if not url:
            LOGGER.warning("Generic webhook enabled but URL env var is missing.")
            return False
        response = requests.post(url, json={"subject": subject, "items": items}, timeout=30)
        response.raise_for_status()
        return True

    def _send_email(self, subject: str, text_body: str) -> bool:
        cfg = self.config.get("notifications", {}).get("smtp", {})
        if not cfg.get("enabled"):
            return False
        host = os.getenv(cfg.get("host_env", "SMTP_HOST"), "")
        port = int(os.getenv(cfg.get("port_env", "SMTP_PORT"), "587"))
        username = os.getenv(cfg.get("username_env", "SMTP_USER"), "")
        password = os.getenv(cfg.get("password_env", "SMTP_PASSWORD"), "")
        sender = os.getenv(cfg.get("from_env", "SMTP_FROM"), username)
        recipients = [x.strip() for x in os.getenv(cfg.get("to_env", "NOTIFY_EMAIL_TO"), "").split(",") if x.strip()]
        if not host or not sender or not recipients:
            LOGGER.warning("SMTP enabled but host/from/to configuration is missing.")
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.set_content(text_body)
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if cfg.get("use_tls", True):
                smtp.starttls()
            if username and password:
                smtp.login(username, password)
            smtp.send_message(msg)
        return True


def normalized_key_part(value: Any) -> str:
    """Normalize a value for use inside a composite item key."""
    return re.sub(r"\s+", " ", html.unescape(str(value or "")).strip()).lower()


def first_raw_value(raw: dict[str, Any], *names: str) -> str:
    """Return the first non-empty raw value, using compact field-name matching."""
    if not raw:
        return ""
    keymap = {compact_key(k): k for k in raw.keys()}
    for name in names:
        original = keymap.get(compact_key(name))
        if original and raw.get(original) not in (None, ""):
            return normalize_key(raw.get(original))
    return ""


def toronto_notification_key(app: dict[str, Any]) -> str:
    """Build a stable application-level Toronto key.

    Rows with the same APPLICATION_URL represent one application and should
    notify once with aggregated addresses. Rows without a link fall back to the
    application/file number and grouped address/date metadata.
    """
    # Prefer the canonical current Application Details URL.  Earlier versions
    # keyed legacy AIC folder URLs directly, so a bad partial run could poison
    # state and prevent the repaired crawler from retrying the same application.
    canonical_url = normalized_key_part(app.get("detail_url"))
    if canonical_url:
        return "toronto:" + stable_hash({"application_url": canonical_url})[:40]

    raw_url = normalized_key_part(app.get("raw_application_url"))
    if raw_url:
        return "toronto:" + stable_hash({"application_url": raw_url})[:40]

    key_payload = {
        "file_number": normalized_key_part(app.get("file_number")),
        "addresses": [normalized_key_part(value) for value in (app.get("addresses") or [])],
        "submitted_date": normalized_key_part(app.get("submitted_date")),
        "application_type": normalized_key_part(app.get("application_type")),
        "status": normalized_key_part(app.get("status")),
    }

    return "toronto:" + stable_hash(key_payload)[:40]


def summarize_sources(items: Iterable[NotificationItem]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.source] = counts.get(item.source, 0) + 1
    return ", ".join(f"{source}={count}" for source, count in sorted(counts.items())) or "none"


def to_notification_items_from_toronto(apps: Iterable[dict[str, Any]]) -> list[NotificationItem]:
    items: list[NotificationItem] = []
    for app in apps:
        addresses = app.get("addresses") or ([app.get("address")] if app.get("address") else [])
        if addresses:
            address_title = addresses[0]
            if len(addresses) > 1:
                address_title = f"{address_title} (+{len(addresses) - 1} more)"
        else:
            address_title = "Toronto development application"

        title_parts = [address_title, app.get("file_number")]
        title = " - ".join(str(part) for part in title_parts if part) or "Toronto development application"
        items.append(
            NotificationItem(
                source="toronto_open_data",
                item_key=toronto_notification_key(app),
                title=title,
                url=app.get("detail_url") or app.get("raw_application_url") or None,
                kind="toronto_open_data",
                payload=app,
            )
        )
    return items


def to_notification_items_from_ottawa(apps: Iterable[dict[str, Any]]) -> list[NotificationItem]:
    items: list[NotificationItem] = []
    for app in apps:
        key = compact_key(app.get("application_number")) or stable_hash(
            {"address": app.get("address"), "date": app.get("application_date")}
        )
        title = ", ".join(app.get("addresses") or []) or app.get("application_number") or "Ottawa development application"
        items.append(
            NotificationItem(
                source="ottawa_devapps",
                item_key=key,
                title=title,
                url=app.get("detail_url"),
                kind="ottawa_devapps",
                payload=app,
            )
        )
    return items


def to_notification_items_from_news(posts: Iterable[dict[str, Any]]) -> list[NotificationItem]:
    items: list[NotificationItem] = []
    for post in posts:
        key = stable_hash({"url": post.get("url"), "title": post.get("title")})
        items.append(
            NotificationItem(
                source=f"news:{post.get('source')}",
                item_key=key,
                title=post.get("title") or "Development news item",
                url=post.get("url"),
                kind="industry_news",
                payload=post,
            )
        )
    return items


def filter_unseen(store: StateStore, items: list[NotificationItem], notify_on_first_run: bool, mark: bool = True) -> list[NotificationItem]:
    source_first_run = {item.source: store.source_is_empty(item.source) for item in items}
    bootstrapped_counts: dict[str, int] = {}
    unseen: list[NotificationItem] = []
    for item in items:
        if store.has_seen(item.source, item.item_key):
            continue
        if mark:
            store.mark_seen(item.source, item.item_key, stable_hash(item.payload))
        if not source_first_run.get(item.source, False) or notify_on_first_run:
            unseen.append(item)
        else:
            bootstrapped_counts[item.source] = bootstrapped_counts.get(item.source, 0) + 1
    if bootstrapped_counts and not notify_on_first_run:
        summary = ", ".join(f"{source}: {count}" for source, count in sorted(bootstrapped_counts.items()))
        LOGGER.info("First run for source(s); bootstrapped item(s) without notification: %s", summary)
    return unseen


def run(config: dict[str, Any], dry_run: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    http = HttpClient(config.get("user_agent", DEFAULT_CONFIG["user_agent"]), int(config.get("request_timeout_seconds", 30)))
    store = StateStore(config["state_db"])
    candidate_items: list[NotificationItem] = []

    ready_items: list[NotificationItem] = []

    if config.get("toronto", {}).get("enabled", True):
        LOGGER.info("Checking Toronto development applications")
        try:
            toronto_monitor = TorontoOpenDataMonitor(http, config["toronto"])
            apps = toronto_monitor.fetch_new_candidates()
            toronto_items = to_notification_items_from_toronto(apps)
            notify_first = bool(config.get("notify_on_first_run", False))
            source_empty = store.source_is_empty("toronto_open_data")

            if source_empty and not notify_first and not dry_run:
                for item in toronto_items:
                    store.mark_seen(item.source, item.item_key, stable_hash(item.payload))
                toronto_unseen: list[NotificationItem] = []
                LOGGER.info(
                    "First Toronto run: bootstrapped %d application(s) without opening detail pages or notifying.",
                    len(toronto_items),
                )
            else:
                toronto_unseen = [item for item in toronto_items if not store.has_seen(item.source, item.item_key)]

            queue_total = len(toronto_unseen)
            queue_limit_key = "dry_run_max_detail_pages" if dry_run else "max_detail_pages_per_run"
            queue_limit = int(config.get("toronto", {}).get(queue_limit_key, 5 if dry_run else 25) or 0)
            if queue_limit > 0 and queue_total > queue_limit:
                toronto_unseen = toronto_unseen[:queue_limit]
                LOGGER.warning(
                    "Toronto detail-page queue bounded to %d of %d unseen application(s) for this run; "
                    "the remainder stay unseen for a later invocation.",
                    len(toronto_unseen),
                    queue_total,
                )

            LOGGER.info("Toronto enrichment queue: %d item(s) need detail-page processing", len(toronto_unseen))
            for index, item in enumerate(toronto_unseen, start=1):
                progress_label = item.title or item.payload.get("file_number") or item.payload.get("address") or "Toronto application"
                LOGGER.info("Toronto enrichment progress %d/%d: %s", index, len(toronto_unseen), progress_label)
                enriched_payload = toronto_monitor.enrich_application(item.payload)
                if enriched_payload.get("enrichment_status") in {"failed", "partial"}:
                    partial_payload = dict(enriched_payload)
                    partial_payload["enrichment_status"] = "partial"
                    partial_key = item.item_key + ":partial"
                    already_reported = store.has_seen(item.source, partial_key)
                    report_partial = bool(config.get("toronto", {}).get("report_partial_on_enrichment_failure", True))
                    if report_partial and (dry_run or not already_reported):
                        partial_item = dataclasses.replace(
                            item,
                            item_key=partial_key,
                            url=partial_payload.get("detail_url") or partial_payload.get("raw_application_url") or item.url,
                            payload=partial_payload,
                        )
                        ready_items.append(partial_item)
                        if not dry_run:
                            store.mark_seen(partial_item.source, partial_item.item_key, stable_hash(partial_payload))
                        LOGGER.warning(
                            "Toronto enrichment incomplete; queued a one-time partial notification and retained the original application for retry: %s; %s",
                            progress_label,
                            partial_payload.get("enrichment_error") or "unknown error",
                        )
                    elif report_partial:
                        LOGGER.warning(
                            "Toronto enrichment remains incomplete; partial notification was already sent and the original application remains queued: %s; %s",
                            progress_label,
                            partial_payload.get("enrichment_error") or "unknown error",
                        )
                    else:
                        LOGGER.warning(
                            "Toronto enrichment incomplete and retained for retry; partial notifications are disabled: %s; %s",
                            progress_label,
                            partial_payload.get("enrichment_error") or "unknown error",
                        )
                    continue
                ready_item = dataclasses.replace(
                    item,
                    url=enriched_payload.get("detail_url") or enriched_payload.get("raw_application_url") or item.url,
                    payload=enriched_payload,
                )
                ready_items.append(ready_item)
                if not dry_run:
                    store.mark_seen(ready_item.source, ready_item.item_key, stable_hash(enriched_payload))
                LOGGER.info(
                    "Toronto enrichment finished %d/%d: %s; documents=%d; owner=%s; applicant=%s",
                    index,
                    len(toronto_unseen),
                    progress_label,
                    sum(1 for value in enriched_payload.get("document_links", {}).values() if normalize_key(value)),
                    "yes" if any(normalize_key(v) for v in (enriched_payload.get("land_owner") or {}).values()) else "no",
                    "yes" if any(normalize_key(v) for v in (enriched_payload.get("applicant") or {}).values()) else "no",
                )

            LOGGER.info(
                "Toronto source yielded %d candidate application(s); %d unseen item(s) required enrichment",
                len(apps),
                len(toronto_unseen),
            )
        except Exception as exc:
            LOGGER.exception("Toronto development application check failed: %s", exc)

    if config.get("ottawa", {}).get("enabled", False):
        LOGGER.info("Checking Ottawa DevApps development applications")
        try:
            ottawa_monitor = OttawaDevAppsMonitor(http, config["ottawa"])
            apps = ottawa_monitor.fetch_new_candidates()
            candidate_items.extend(to_notification_items_from_ottawa(apps))
            LOGGER.info("Ottawa DevApps yielded %d candidate application(s)", len(apps))
        except Exception as exc:
            LOGGER.exception("Ottawa DevApps check failed: %s", exc)

    if config.get("news", {}).get("enabled", True):
        LOGGER.info("Checking industry news sources")
        try:
            news_monitor = NewsMonitor(http, config["news"])
            posts = news_monitor.fetch_relevant_posts()
            candidate_items.extend(to_notification_items_from_news(posts))
            LOGGER.info("News sources yielded %d relevant candidate post(s)", len(posts))
        except Exception as exc:
            LOGGER.exception("News check failed: %s", exc)

    LOGGER.info(
        "All unfiltered sources yielded %d candidate item(s): %s",
        len(candidate_items),
        summarize_sources(candidate_items),
    )
    unseen = ready_items + filter_unseen(
        store,
        candidate_items,
        bool(config.get("notify_on_first_run", False)),
        mark=not dry_run,
    )
    LOGGER.info("After state filtering, %d new item(s) remain for notification: %s", len(unseen), summarize_sources(unseen))
    Notifier(config, dry_run=dry_run).send(unseen)
    return 0


def diagnose_toronto_url(config: dict[str, Any], url: str) -> int:
    """Run one Toronto detail page without reading or modifying SQLite state."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    http = HttpClient(
        config.get("user_agent", DEFAULT_CONFIG["user_agent"]),
        int(config.get("request_timeout_seconds", 30)),
    )
    monitor = TorontoOpenDataMonitor(http, config.get("toronto", {}))
    canonical = monitor._application_details_url(url)
    payload = {
        "raw_application_url": url,
        "detail_url": canonical,
        "address": parse_qs(urlparse(url).query).get("title", [""])[0],
        "file_number": "",
    }
    result = monitor.enrich_application(payload)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("enrichment_status") == "ok" else 2


def forget_toronto_url(config: dict[str, Any], url: str) -> int:
    """Remove supplied/canonical URL keys so the regular monitor retries them."""
    http = HttpClient(
        config.get("user_agent", DEFAULT_CONFIG["user_agent"]),
        int(config.get("request_timeout_seconds", 30)),
    )
    monitor = TorontoOpenDataMonitor(http, config.get("toronto", {}))
    canonical = monitor._application_details_url(url) or url
    variants = {url, canonical}
    parsed = urlparse(canonical)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query:
        variants.add(parsed._replace(query=urlencode({k: v[0] for k, v in query.items()})).geturl())
    keys = {
        "toronto:" + stable_hash({"application_url": normalized_key_part(value)})[:40]
        for value in variants if normalize_key(value)
    }
    deleted = StateStore(config["state_db"]).delete_seen("toronto_open_data", keys)
    print(f"Removed {deleted} matching Toronto seen-state row(s).")
    if not deleted:
        print("No exact URL key matched. Use --diagnose-url to test without state, or inspect the SQLite seen_items table.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor Toronto/Ottawa development applications and construction/development news.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file")
    parser.add_argument("--dry-run", action="store_true", help="Print notifications instead of sending them")
    parser.add_argument(
        "--diagnose-url",
        help="Render and diagnose one Toronto application URL without touching the state database",
    )
    parser.add_argument(
        "--forget-url",
        help="Remove a Toronto application URL from SQLite seen state so the normal monitor retries it",
    )
    args = parser.parse_args(argv)
    config_path = args.config if os.path.exists(args.config) else None
    if args.config and not config_path:
        print(f"Config {args.config!r} not found; using defaults.", file=sys.stderr)
    config = load_config(config_path)
    if args.forget_url:
        return forget_toronto_url(config, args.forget_url)
    if args.diagnose_url:
        return diagnose_toronto_url(config, args.diagnose_url)
    return run(config, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
