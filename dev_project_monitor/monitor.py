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
import smtplib
import sqlite3
import sys
import tempfile
import textwrap
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
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
        "raw_record_limit": 0,
        "render_with_playwright": True,
        "page_timeout_ms": 60000,
        "post_load_wait_ms": 2500,
        "ocr_image_pdfs": True,
        "extract_application_form_contacts": True,
        "application_form_download_timeout_ms": 12000,
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

        grouped = self._group_rows_by_application(normalized_rows)
        grouped.sort(
            key=lambda x: parse_dt(x.get("submitted_date") or x.get("last_updated"))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
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
        for group_key, group_rows in groups.items():
            representative = self._choose_representative_row(group_rows)
            addresses = self._unique_sorted(row.get("address") for row in group_rows)
            csv_row_ids = self._unique_sorted(row.get("csv_row_id") for row in group_rows)
            file_numbers = self._unique_sorted(row.get("file_number") for row in group_rows)

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
        url = normalize_key(url)
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
        candidate = normalize_key(candidate)
        if not candidate:
            return False
        lowered = candidate.lower()
        if lowered.startswith(("blob:", "about:", "javascript:", "mailto:", "tel:", "#")):
            return False
        full_url = urljoin(base_url, candidate)
        if self._is_static_asset_url(full_url):
            return False

        base_no_hash = normalize_key(base_url).split("#", 1)[0]
        full_no_hash = full_url.split("#", 1)[0]
        if base_no_hash and full_no_hash == base_no_hash:
            return False

        parsed = urlparse(full_url)
        if "/application-details/" in parsed.path and not re.search(r"download|document|attachment|file", parsed.query, re.I):
            return False

        return True

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
        """Render Toronto application page and expand/capture Supporting Documentation."""
        if self.config.get("render_with_playwright", True):
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1365, "height": 2400},
                        user_agent=self.http.session.headers.get("User-Agent"),
                        accept_downloads=True,
                    )
                    try:
                        page = context.new_page()
                        network_recorder = self._install_toronto_network_recorder(page)

                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=int(self.config.get("page_timeout_ms", 60000)),
                        )

                        # Do not wait a long time for networkidle. The Toronto page can keep
                        # analytics/API requests open, and this monitor only needs the rendered
                        # Supporting Documentation table.
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass

                        page.wait_for_timeout(int(self.config.get("post_load_wait_ms", 2500)))

                        # A valid Toronto application page usually shows these labels, but do not
                        # fail here: the app can render the document table before all labels settle.
                        try:
                            page.locator("body").filter(
                                has_text=re.compile(
                                    r"Application Number|Application Status|Supporting Documentation|Reference File|Download",
                                    re.I,
                                )
                            ).wait_for(timeout=30000)
                        except Exception:
                            pass

                        self._expand_supporting_documentation(page)

                        for scope in self._document_scopes(page):
                            try:
                                self._set_document_table_to_all_rows(scope)
                            except Exception as exc:
                                LOGGER.info("Could not switch document table to all/100 entries for %s: %s", url, exc)

                        # Scroll so lazy-rendered table content appears.
                        try:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(1000)
                        except Exception:
                            pass

                        final_url = page.url
                        network_links, network_available = self._extract_network_document_data(network_recorder, final_url)
                        button_links, button_available = self._extract_download_button_document_rows(page, final_url)

                        # Fast path: do not click Download buttons. Toronto exposes the
                        # important row identity as button.downloadFile[data-id]. Clicking
                        # each button can trigger browser downloads and several-second
                        # waits per document per application, which makes scheduled runs
                        # unacceptably slow. Prefer real network URLs seen during page
                        # load, then fall back to stable row-specific data-id URLs.
                        document_links = dict(network_links)
                        for name, href in button_links.items():
                            if href and not document_links.get(name):
                                document_links[name] = href

                        visible_available = self._document_names_visible_on_page(page)
                        available_names = (
                            set(network_available)
                            | set(button_available)
                            | set(visible_available)
                            | set(document_links)
                        )

                        html_text = self._combined_page_content(page)
                        html_text += self._download_links_fixture_html(document_links, available_names, final_url)

                        try:
                            doc_row_count = sum(self._document_row_count(scope) for scope in self._document_scopes(page))
                        except Exception:
                            doc_row_count = "unknown"

                        application_form_text = ""
                        if self.config.get("extract_application_form_contacts", True) and "Application Form" in available_names:
                            application_form_text = self._extract_application_form_text_from_page(page, final_url)
                            if application_form_text:
                                html_text += self._application_form_text_fixture_html(application_form_text)

                        LOGGER.info("Rendered Toronto detail page length for %s: %s", final_url, len(html_text or ""))
                        LOGGER.info(
                            "Supporting Documentation for %s: rows=%s; direct_links=%s; available_rows=%s; application_form_text=%s; names=%s",
                            final_url,
                            doc_row_count,
                            len(document_links),
                            len(available_names),
                            bool(normalize_key(application_form_text)),
                            ", ".join(sorted(available_names)) or "none",
                        )

                        return final_url, html_text
                    finally:
                        context.close()
                        browser.close()

            except Exception as exc:
                LOGGER.info(
                    "Playwright render unavailable for %s; falling back to requests: %s",
                    url,
                    exc,
                )

        response = self.http.get(url)
        return response.url, response.text

    def _document_scopes(self, page: Any) -> list[Any]:
        scopes: list[Any] = [page]
        try:
            for frame in page.frames:
                if frame is not page.main_frame:
                    scopes.append(frame)
        except Exception:
            pass
        return scopes

    def _combined_page_content(self, page: Any) -> str:
        parts: list[str] = []
        for scope in self._document_scopes(page):
            try:
                parts.append(scope.content())
            except Exception:
                continue
        return "\n".join(parts)

    def _expand_supporting_documentation(self, page: Any) -> None:
        labels = (
            r"Expand\s+All",
            r"Supporting\s+Documentation",
            r"Supporting\s+Documents",
            r"Reference\s+File",
        )
        for scope in self._document_scopes(page):
            for label_pattern in labels:
                pattern = re.compile(label_pattern, re.I)
                for locator_getter in (
                    lambda: scope.get_by_role("button", name=pattern),
                    lambda: scope.get_by_role("link", name=pattern),
                    lambda: scope.get_by_text(pattern),
                ):
                    try:
                        matches = locator_getter()
                        count = min(matches.count(), 8)
                    except Exception:
                        continue
                    for i in range(count):
                        try:
                            matches.nth(i).click(timeout=3000, no_wait_after=True)
                            page.wait_for_timeout(700)
                        except Exception:
                            continue

        # Give the table a chance to render after expansion.
        try:
            page.wait_for_timeout(1500)
        except Exception:
            pass

    def _document_row_count(self, scope: Any) -> int:
        try:
            return int(
                scope.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('table tbody tr, [role="row"]'))
                        .filter(tr => /Download|Reference File|Application Form|Architectural|Civil|Geotechnical|Hydro/i.test(tr.innerText || ''))
                        .length
                    """
                )
            )
        except Exception:
            return 0

    def _install_toronto_network_recorder(self, page: Any) -> dict[str, Any]:
        recorder: dict[str, Any] = {"responses": [], "download_urls": []}

        def on_response(response: Any) -> None:
            try:
                headers = {k.lower(): v for k, v in (response.headers or {}).items()}
                url = normalize_key(response.url)
                if self._response_looks_like_download(url, headers):
                    recorder.setdefault("download_urls", []).append(url)
                recorder.setdefault("responses", []).append(response)
            except Exception:
                return

        try:
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

    def _extract_network_document_data(self, recorder: dict[str, Any], base_url: str) -> tuple[dict[str, str], set[str]]:
        links: dict[str, str] = {}
        available: set[str] = set()

        for response in recorder.get("responses", [])[:500]:
            try:
                url = normalize_key(response.url)
                headers = {k.lower(): v for k, v in (response.headers or {}).items()}
                request = getattr(response, "request", None)
                resource_type = getattr(request, "resource_type", "") if request else ""
            except Exception:
                continue

            lowered_url = url.lower()
            content_type = normalize_key(headers.get("content-type", "")).lower()
            relevant = (
                resource_type in {"xhr", "fetch", "document"}
                or "json" in content_type
                or any(token in lowered_url for token in ("aic", "application", "document", "attachment", "file", "folder", "submission"))
            )
            if not relevant:
                continue

            # File responses are useful as direct URLs, but they usually do not identify
            # which row they came from unless captured during a row click.
            if self._response_looks_like_download(url, headers) and self._is_meaningful_document_url(url, base_url):
                continue

            try:
                body_text = response.text()
            except Exception:
                body_text = ""
            if not body_text or len(body_text) > 2_500_000:
                continue

            found_links, found_available = self._extract_document_data_from_text(body_text, url, base_url)
            for name in found_available:
                available.add(name)
            for name, href in found_links.items():
                if href and not links.get(name):
                    links[name] = href

        return links, available

    def _download_button_fallback_url(self, base_url: str, document_name: str, data_id: str = "") -> str:
        """Return a stable URL for a Toronto Supporting Documentation row.

        The Toronto page renders document rows as DataTables rows with a
        button.downloadFile[data-id] control. The data-id is a JavaScript token,
        not an href. When the real download URL is not exposed by the network
        layer, this URL still opens the correct application details page and
        carries the document/data-id in the fragment for debugging and manual
        lookup.
        """
        base = normalize_key(base_url).split("#", 1)[0]
        if not base:
            return ""
        fragment_parts = ["supporting-documentation"]
        if document_name:
            fragment_parts.append("document=" + quote_plus(normalize_key(document_name)))
        if data_id:
            fragment_parts.append("data-id=" + quote_plus(normalize_key(data_id)))
        return base + "#" + "&".join(fragment_parts)

    def _extract_download_button_document_rows(self, page: Any, base_url: str) -> tuple[dict[str, str], set[str]]:
        """Extract document rows from DataTables, including hidden/paginated rows.

        Toronto's Supporting Documentation table stores the document name in the
        first cell and the action in the fourth cell as:

            <button class="downloadFile" data-id="...">Download</button>

        DataTables often keeps only the current 10 DOM rows visible. Reading the
        DataTables API gives all rows, including rows on later pages and rows
        found through the table search box.
        """
        links: dict[str, str] = {}
        available: set[str] = set()
        rows_seen = 0

        js = r"""
        () => {
            const out = [];
            const seen = new Set();

            const textFromHtml = (value) => {
                const raw = String(value ?? '');
                const div = document.createElement('div');
                div.innerHTML = raw;
                return (div.innerText || div.textContent || raw).replace(/\s+/g, ' ').trim();
            };

            const attrsFromHtml = (value) => {
                const raw = String(value ?? '');
                const attrs = [];
                const div = document.createElement('div');
                div.innerHTML = raw;
                for (const node of div.querySelectorAll('a, button, input, [href], [src], [onclick], [data-id], [data-url], [data-href], [data-download-url], [data-document-url], [data-file-url], [data-attachment-url]')) {
                    const rec = {};
                    if (!node.getAttributeNames) continue;
                    for (const attr of node.getAttributeNames()) {
                        const lower = attr.toLowerCase();
                        if (['href', 'src', 'formaction', 'onclick'].includes(lower) || lower.startsWith('data')) {
                            rec[lower] = node.getAttribute(attr);
                        }
                    }
                    const cls = node.getAttribute('class');
                    if (cls) rec.class = cls;
                    if (Object.keys(rec).length) attrs.push(rec);
                }

                // Be tolerant of escaped HTML strings returned by DataTables.
                for (const match of raw.matchAll(/data-id\s*=\s*["']([^"']+)["']/gi)) {
                    attrs.push({'data-id': match[1], class: 'downloadFile'});
                }
                for (const match of raw.matchAll(/href\s*=\s*["']([^"']+)["']/gi)) {
                    attrs.push({href: match[1]});
                }
                return attrs;
            };

            const attrsFromNode = (root) => {
                const attrs = [];
                const nodes = [root, ...root.querySelectorAll('a, button, input, [href], [src], [onclick], [data-id], [data-url], [data-href], [data-download-url], [data-document-url], [data-file-url], [data-attachment-url]')];
                for (const node of nodes) {
                    if (!node || !node.getAttributeNames) continue;
                    const rec = {};
                    for (const attr of node.getAttributeNames()) {
                        const lower = attr.toLowerCase();
                        if (['href', 'src', 'formaction', 'onclick'].includes(lower) || lower.startsWith('data')) {
                            rec[lower] = node.getAttribute(attr);
                        }
                    }
                    const cls = node.getAttribute('class');
                    if (cls) rec.class = cls;
                    if (Object.keys(rec).length) attrs.push(rec);
                }
                return attrs;
            };

            const addRow = (source, cells, attrs) => {
                const safeCells = (cells || []).map(v => String(v ?? ''));
                const text = safeCells.map(textFromHtml).join(' ').replace(/\s+/g, ' ').trim();
                const html = safeCells.join(' ');
                const dataIds = [];
                const hrefs = [];
                const onclicks = [];

                for (const rec of attrs || []) {
                    for (const [key, value] of Object.entries(rec)) {
                        const lower = String(key).toLowerCase();
                        const val = String(value ?? '').trim();
                        if (!val) continue;
                        if (lower === 'href' || lower === 'src' || lower === 'formaction' || lower.includes('url') || lower.includes('href')) hrefs.push(val);
                        if (lower === 'onclick') onclicks.push(val);
                        if (lower === 'data-id' || lower.endsWith('-id') || lower === 'id') dataIds.push(val);
                    }
                }

                if (!text && !html && !dataIds.length && !hrefs.length) return;
                const key = [source, text, dataIds.join('|'), hrefs.join('|')].join('||');
                if (seen.has(key)) return;
                seen.add(key);
                out.push({source, text, html, data_ids: dataIds, hrefs, onclicks});
            };

            const tables = Array.from(document.querySelectorAll('table'));

            if (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable) {
                for (const table of tables) {
                    try {
                        if (window.jQuery.fn.dataTable.isDataTable && !window.jQuery.fn.dataTable.isDataTable(table)) continue;
                        const dt = window.jQuery(table).DataTable();
                        const data = dt.rows().data().toArray();
                        for (const row of data.slice(0, 1000)) {
                            let cells = [];
                            if (Array.isArray(row)) cells = row;
                            else if (row && typeof row === 'object') cells = Object.values(row);
                            else cells = [row];
                            const attrs = [];
                            for (const cell of cells) attrs.push(...attrsFromHtml(cell));
                            addRow('datatable', cells, attrs);
                        }
                    } catch (e) {}
                }
            }

            // DOM fallback for the currently visible page of the table.
            for (const tr of document.querySelectorAll('table tbody tr, table tr')) {
                const cells = Array.from(tr.children || []).map(td => td.innerHTML || td.textContent || '');
                addRow('dom', cells.length ? cells : [tr.innerHTML || tr.textContent || ''], attrsFromNode(tr));
            }

            return out.slice(0, 1200);
        }
        """

        for scope in self._document_scopes(page):
            try:
                raw_rows = scope.evaluate(js) or []
            except Exception as exc:
                LOGGER.info("Could not inspect Toronto DataTables document rows for %s: %s", base_url, exc)
                raw_rows = []

            for row in raw_rows:
                rows_seen += 1
                text = normalize_key((row or {}).get("text") or (row or {}).get("html"))
                if not text or "no matching records" in text.lower():
                    continue

                document_name = self._document_name_for_text(text)
                if not document_name:
                    continue
                available.add(document_name)

                for href in (row or {}).get("hrefs") or []:
                    full_url = urljoin(base_url, normalize_key(href))
                    if self._is_meaningful_document_url(full_url, base_url):
                        links.setdefault(document_name, full_url)
                        break

                if not links.get(document_name):
                    data_ids = [normalize_key(value) for value in ((row or {}).get("data_ids") or []) if normalize_key(value)]
                    # Prefer tokens attached to the visible Download button. Ignore generic table IDs.
                    if data_ids:
                        links.setdefault(document_name, self._download_button_fallback_url(base_url, document_name, data_ids[0]))

        if available:
            LOGGER.info(
                "Detected Toronto document button rows for %s: rows_inspected=%s; required=%s; token_links=%s",
                base_url,
                rows_seen,
                ", ".join(sorted(available)),
                ", ".join(sorted(links)) or "none",
            )
        else:
            LOGGER.info("No required Toronto document button rows detected for %s after inspecting %s row(s)", base_url, rows_seen)

        return links, available

    def _extract_document_data_from_text(self, body_text: str, source_url: str, base_url: str) -> tuple[dict[str, str], set[str]]:
        links: dict[str, str] = {}
        available: set[str] = set()
        text_sample = body_text[:2_500_000]

        looks_like_document_table = bool(re.search(r"Reference\s+File|Supporting\s+Documentation|\bDownload\b", text_sample, re.I))
        if looks_like_document_table:
            for name, patterns in self.REQUIRED_DOCUMENTS.items():
                if any(re.search(pattern, text_sample, flags=re.I) for pattern in patterns):
                    available.add(name)

        if "<" in text_sample and ("Download" in text_sample or "Reference File" in text_sample or "href" in text_sample):
            try:
                html_links = self._extract_document_links(text_sample, base_url)
                for name, href in html_links.items():
                    if href and self._is_meaningful_document_url(href, base_url):
                        links[name] = href
                        available.add(name)
            except Exception:
                pass

        try:
            data = json.loads(text_sample)
        except Exception:
            data = None

        if data is not None:
            self._walk_document_json(data, source_url, base_url, links, available)

        return links, available

    def _walk_document_json(
        self,
        obj: Any,
        source_url: str,
        base_url: str,
        links: dict[str, str],
        available: set[str],
        depth: int = 0,
    ) -> None:
        if depth > 8:
            return

        if isinstance(obj, dict):
            scalar_values: list[str] = []
            for key, value in obj.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    scalar_values.append(f"{key}: {value}")
            combined = normalize_key(" ".join(scalar_values))
            doc_name = self._document_name_for_text(combined)
            if doc_name:
                available.add(doc_name)
                for candidate in self._json_url_candidates(obj, source_url, base_url):
                    if self._is_meaningful_document_url(candidate, base_url):
                        links.setdefault(doc_name, candidate)
                        break

            for value in obj.values():
                self._walk_document_json(value, source_url, base_url, links, available, depth + 1)
            return

        if isinstance(obj, list):
            for value in obj[:300]:
                self._walk_document_json(value, source_url, base_url, links, available, depth + 1)
            return

        if isinstance(obj, str):
            doc_name = self._document_name_for_text(obj)
            if doc_name:
                available.add(doc_name)
                if "<" in obj or "http" in obj or "/" in obj:
                    found_links, found_available = self._extract_document_data_from_text(obj, source_url, base_url)
                    available.update(found_available)
                    for name, href in found_links.items():
                        links.setdefault(name, href)

    def _json_url_candidates(self, obj: dict[str, Any], source_url: str, base_url: str) -> list[str]:
        candidates: list[str] = []

        def add(value: Any) -> None:
            raw = html.unescape(str(value or "")).strip()
            if not raw:
                return
            pieces: list[str] = []
            pieces.extend(re.findall(r"https?://[^\s'\"<>\\)]+", raw, flags=re.I))
            pieces.extend(re.findall(r"(?<![A-Za-z0-9])(/[^\s'\"<>\\)]+)", raw))
            if not pieces and re.search(r"\.(?:pdf|zip|docx?|xlsx?)(?:\?|$)|download|document|attachment|file", raw, re.I):
                pieces.append(raw)
            for piece in pieces:
                full_url = urljoin(base_url or source_url, normalize_key(piece))
                if full_url and full_url not in candidates:
                    candidates.append(full_url)

        for key, value in obj.items():
            key_lower = str(key).lower()
            if any(token in key_lower for token in ("url", "href", "link", "download", "document", "attachment", "file", "path")):
                add(value)
            elif isinstance(value, str) and re.search(r"https?://|/.*(?:download|document|attachment|file)|\.(?:pdf|zip|docx?|xlsx?)(?:\?|$)", value, re.I):
                add(value)

        return candidates

    def _set_document_table_to_all_rows(self, scope: Any, search_term: str = "") -> None:
        """Make the Supporting Documentation table expose matching rows in the DOM."""
        scope.evaluate(
            """
            (searchTerm) => {
                const terms = (searchTerm || '').trim();

                const fire = (el, type) => el.dispatchEvent(new Event(type, { bubbles: true }));

                if (window.jQuery && window.jQuery.fn && window.jQuery.fn.dataTable) {
                    window.jQuery('table').each(function () {
                        try {
                            const dt = window.jQuery(this).DataTable();
                            if (dt.search) dt.search(terms);
                            if (dt.page && dt.page.len) dt.page.len(100).draw(false);
                        } catch (e) {}
                    });
                }

                if (window.DataTable && window.DataTable.tables) {
                    try {
                        for (const table of window.DataTable.tables({visible: true})) {
                            try {
                                const dt = new window.DataTable(table);
                                if (dt.search) dt.search(terms);
                                if (dt.page && dt.page.len) dt.page.len(100).draw(false);
                            } catch (e) {}
                        }
                    } catch (e) {}
                }

                const searchInputs = Array.from(document.querySelectorAll(
                    'input[type="search"], input[aria-label*="Search" i], input[placeholder*="Search" i]'
                ));
                for (const input of searchInputs) {
                    input.focus();
                    input.value = terms;
                    fire(input, 'input');
                    fire(input, 'keyup');
                    fire(input, 'change');
                }

                for (const sel of document.querySelectorAll('select')) {
                    const opts = Array.from(sel.options || []);
                    if (!opts.length) continue;
                    const numeric = opts
                        .map(o => ({ option: o, n: parseInt(o.value || o.textContent || '0', 10) }))
                        .filter(x => !Number.isNaN(x.n));
                    const chosen =
                        opts.find(o => o.value === '-1') ||
                        opts.find(o => o.value === '100') ||
                        opts.find(o => /100/.test(o.textContent || '')) ||
                        (numeric.length ? numeric.sort((a, b) => b.n - a.n)[0].option : null) ||
                        opts[opts.length - 1];
                    if (chosen) {
                        sel.value = chosen.value;
                        fire(sel, 'input');
                        fire(sel, 'change');
                    }
                }
            }
            """,
            search_term,
        )
        try:
            scope.wait_for_timeout(1000)
        except Exception:
            pass

    def _row_url_candidates(self, row: Any, base_url: str) -> list[str]:
        try:
            raw_values = row.evaluate(
                """
                (row) => {
                    const values = [];
                    const add = (value) => {
                        if (value !== undefined && value !== null && String(value).trim()) {
                            values.push(String(value));
                        }
                    };
                    const nodes = [row, ...row.querySelectorAll('a, button, input, [onclick], [href], [src], [formaction], [data-href], [data-url], [data-download-url], [data-document-url], [data-file-url], [data-attachment-url]')];
                    for (const node of nodes) {
                        if (!node.getAttributeNames) continue;
                        for (const attr of node.getAttributeNames()) {
                            const lower = attr.toLowerCase();
                            if (['href', 'src', 'formaction', 'onclick'].includes(lower) || lower.startsWith('data')) {
                                add(node.getAttribute(attr));
                            }
                        }
                    }
                    return values;
                }
                """
            )
        except Exception:
            raw_values = []

        candidates: list[str] = []
        for value in raw_values or []:
            raw = html.unescape(str(value or "")).strip()
            if not raw:
                continue
            pieces: list[str] = []
            pieces.extend(re.findall(r"https?://[^\s'\"<>\\)]+", raw, flags=re.I))
            pieces.extend(re.findall(r"(?<![A-Za-z0-9])(/[^\s'\"<>\\)]+)", raw))
            pieces.extend(
                re.findall(
                    r"['\"]([^'\"]*(?:download|document|attachment|file)[^'\"]*)['\"]",
                    raw,
                    flags=re.I,
                )
            )
            if not pieces:
                lowered = raw.lower()
                if not lowered.startswith(("javascript:", "mailto:", "tel:", "#", "blob:")) and (
                    "/" in raw or "download" in lowered or "document" in lowered or "file" in lowered or "attachment" in lowered
                ):
                    pieces.append(raw)
            for piece in pieces:
                full_url = urljoin(base_url, normalize_key(piece))
                if self._is_meaningful_document_url(full_url, base_url) and full_url not in candidates:
                    candidates.append(full_url)
        return candidates

    def _visible_document_rows(self, scope: Any, required_name: str) -> list[Any]:
        rows: list[Any] = []
        selectors = "table tbody tr, table tr, [role='row']"
        try:
            locator = scope.locator(selectors)
            count = min(locator.count(), 300)
        except Exception:
            return rows

        for index in range(count):
            row = locator.nth(index)
            try:
                text = normalize_key(row.inner_text(timeout=1200))
            except Exception:
                continue
            if not text or "no matching records" in text.lower():
                continue
            if self._document_name_for_text(text) == required_name:
                rows.append(row)
        return rows

    def _document_names_visible_on_page(self, page: Any) -> set[str]:
        names: set[str] = set()
        for scope in self._document_scopes(page):
            try:
                texts = scope.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('table tbody tr, table tr, [role="row"], li'))
                        .map(el => el.innerText || el.textContent || '')
                        .filter(Boolean)
                        .slice(0, 500)
                    """
                )
            except Exception:
                texts = []
            for text in texts or []:
                name = self._document_name_for_text(text)
                if name:
                    names.add(name)
        return names

    def _click_row_download_for_url(self, page: Any, row: Any, base_url: str, required_name: str) -> str:
        downloads: list[Any] = []
        popups: list[Any] = []
        opened_urls: list[str] = []
        response_candidates: list[str] = []
        request_candidates: list[str] = []

        def useful_url(candidate: str | None) -> str:
            candidate = normalize_key(candidate)
            if not candidate:
                return ""
            full_url = urljoin(base_url, candidate)
            return full_url if self._is_meaningful_document_url(full_url, base_url) else ""

        def on_download(download: Any) -> None:
            downloads.append(download)

        def on_popup(popup: Any) -> None:
            popups.append(popup)

        def on_request(request: Any) -> None:
            try:
                request_url = normalize_key(request.url)
                resource_type = normalize_key(getattr(request, "resource_type", "")).lower()
                method = normalize_key(getattr(request, "method", "")).upper()
            except Exception:
                return
            if self._is_static_asset_url(request_url):
                return
            lowered = request_url.lower()
            if resource_type in {"xhr", "fetch", "document"} or method == "POST" or any(
                token in lowered for token in ("download", "document", "attachment", "file", "aic", "application", "folder")
            ):
                request_candidates.append(request_url)

        def on_response(response: Any) -> None:
            try:
                response_url = normalize_key(response.url)
                headers = {k.lower(): v for k, v in (response.headers or {}).items()}
            except Exception:
                return
            if self._is_static_asset_url(response_url):
                return
            if self._response_looks_like_download(response_url, headers):
                response_candidates.append(response_url)
                return
            lowered = response_url.lower()
            if any(token in lowered for token in ("download", "document", "attachment", "file")):
                response_candidates.append(response_url)

        try:
            page.on("download", on_download)
            page.on("popup", on_popup)
            page.on("request", on_request)
            page.on("response", on_response)
        except Exception:
            pass

        before_url = page.url
        try:
            try:
                page.evaluate(
                    """
                    () => {
                        if (!window.__torontoCapturedOpenInstalled) {
                            window.__torontoCapturedOpenInstalled = true;
                            window.__torontoCapturedOpens = [];
                            const originalOpen = window.open;
                            window.open = function(url, ...args) {
                                try { window.__torontoCapturedOpens.push(String(url || '')); } catch (e) {}
                                if (originalOpen) return originalOpen.call(window, url, ...args);
                                return null;
                            };
                        } else {
                            window.__torontoCapturedOpens = [];
                        }
                    }
                    """
                )
            except Exception:
                pass

            control = row.locator("a:has-text('Download'), button:has-text('Download'), input[value*='Download' i]").first
            try:
                if control.count() == 0:
                    control = row.locator("a, button, input[type='button'], input[type='submit'], [role='button']").first
            except Exception:
                pass

            clicked = False
            try:
                control.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass

            try:
                with page.expect_download(timeout=6000) as download_info:
                    control.click(timeout=5000, no_wait_after=True, force=True)
                    clicked = True
                download = download_info.value
                candidate = useful_url(getattr(download, "url", ""))
                try:
                    download.cancel()
                except Exception:
                    pass
                if candidate:
                    return candidate
            except Exception:
                if not clicked:
                    try:
                        control.click(timeout=5000, no_wait_after=True, force=True)
                        clicked = True
                    except Exception as exc:
                        LOGGER.info("Could not click %s document download button: %s", required_name, exc)
                        return ""

            page.wait_for_timeout(3000)

            for download in reversed(downloads):
                candidate = useful_url(getattr(download, "url", ""))
                try:
                    download.cancel()
                except Exception:
                    pass
                if candidate:
                    return candidate

            for popup in reversed(popups):
                try:
                    popup.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                candidate = useful_url(getattr(popup, "url", ""))
                try:
                    popup.close()
                except Exception:
                    pass
                if candidate:
                    return candidate

            try:
                opened_urls = page.evaluate("window.__torontoCapturedOpens || []") or []
            except Exception:
                opened_urls = []
            for candidate in reversed(opened_urls):
                candidate = useful_url(candidate)
                if candidate:
                    return candidate

            candidate = useful_url(page.url)
            if candidate and candidate != before_url:
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=5000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                return candidate

            for candidate in reversed(response_candidates):
                candidate = useful_url(candidate)
                if candidate:
                    return candidate

            # Last click-level fallback: if the click produced exactly one non-static XHR/POST,
            # return it. This catches city endpoints that stream a file without helpful headers.
            unique_requests = []
            for candidate in request_candidates:
                candidate = useful_url(candidate)
                if candidate and candidate not in unique_requests:
                    unique_requests.append(candidate)
            if len(unique_requests) == 1:
                return unique_requests[0]
        finally:
            try:
                page.remove_listener("download", on_download)
                page.remove_listener("popup", on_popup)
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)
            except Exception:
                pass

        return ""

    def _capture_document_download_links(self, page: Any, base_url: str) -> tuple[dict[str, str], set[str]]:
        """Capture URLs hidden behind Toronto's JavaScript Download buttons."""
        captured: dict[str, str] = {}
        available: set[str] = set()
        search_terms = {
            "Application Form": ["Application Form", "Application"],
            "Architectural Plans": ["Architectural Plans", "Architectural", "Elevations", "Floor Plans"],
            "Civil and Utilities Plans": ["Civil and Utilities Plans", "Utilities", "Civil", "Servicing", "Grading", "Stormwater"],
            "Geotechnical Study": ["Geotechnical Study", "Geotechnical", "Geo-tech", "Soil"],
            "Hydrogeological Report": ["Hydrogeological Report", "Hydrogeological", "Hydrogeology", "Groundwater", "Dewatering"],
        }

        scopes = self._document_scopes(page)
        for scope in scopes:
            try:
                self._set_document_table_to_all_rows(scope)
            except Exception:
                pass

        for required_name in self.REQUIRED_DOCUMENTS:
            rows: list[Any] = []
            for scope in scopes:
                rows.extend(self._visible_document_rows(scope, required_name))
            if not rows:
                for term in search_terms.get(required_name, [required_name]):
                    for scope in scopes:
                        try:
                            self._set_document_table_to_all_rows(scope, term)
                        except Exception:
                            continue
                    rows = []
                    for scope in scopes:
                        rows.extend(self._visible_document_rows(scope, required_name))
                    if rows:
                        break

            if rows:
                available.add(required_name)

            for row in rows[:3]:
                for candidate in self._row_url_candidates(row, base_url):
                    if candidate:
                        captured[required_name] = candidate
                        break
                if captured.get(required_name):
                    break

                clicked_url = self._click_row_download_for_url(page, row, base_url, required_name)
                if clicked_url:
                    captured[required_name] = clicked_url
                    break

            for scope in scopes:
                try:
                    self._set_document_table_to_all_rows(scope)
                except Exception:
                    pass

        if captured:
            LOGGER.info("Captured %d Toronto direct document link(s) for %s: %s", len(captured), base_url, ", ".join(sorted(captured)))
        if available:
            LOGGER.info("Detected %d Toronto required document row(s) for %s: %s", len(available), base_url, ", ".join(sorted(available)))
        if not captured and not available:
            LOGGER.info("No Toronto Supporting Documentation rows or direct document links captured for %s", base_url)
        return captured, available

    def _extract_application_form_text_from_page(self, page: Any, base_url: str) -> str:
        """Click only the Application Form download button and extract its text.

        Other document rows are detected from DataTables/data-id tokens without
        clicking. The Application Form is the one exception because owner and
        applicant fields live inside the PDF/form itself.
        """
        timeout_ms = int(self.config.get("application_form_download_timeout_ms", 12000) or 12000)
        scopes = self._document_scopes(page)
        rows: list[Any] = []

        for scope in scopes:
            try:
                self._set_document_table_to_all_rows(scope)
            except Exception:
                pass
        for scope in scopes:
            rows.extend(self._visible_document_rows(scope, "Application Form"))

        if not rows:
            for term in ("Application Form", "Application"):
                for scope in scopes:
                    try:
                        self._set_document_table_to_all_rows(scope, term)
                    except Exception:
                        pass
                rows = []
                for scope in scopes:
                    rows.extend(self._visible_document_rows(scope, "Application Form"))
                if rows:
                    break

        if not rows:
            LOGGER.info("Application Form row not visible for %s; contact extraction skipped", base_url)
            return ""

        for row in rows[:2]:
            try:
                control = row.locator("button.downloadFile, a:has-text('Download'), button:has-text('Download'), input[value*='Download' i]").first
                try:
                    if control.count() == 0:
                        control = row.locator("a, button, input[type='button'], input[type='submit'], [role='button']").first
                except Exception:
                    pass

                try:
                    control.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass

                with page.expect_download(timeout=timeout_ms) as download_info:
                    control.click(timeout=5000, no_wait_after=True, force=True)
                download = download_info.value

                file_bytes = self._read_playwright_download_bytes(download)
                suggested_name = normalize_key(getattr(download, "suggested_filename", ""))
                try:
                    download.delete()
                except Exception:
                    pass

                text = self._extract_application_form_text_from_bytes(file_bytes, suggested_name)
                if normalize_key(text):
                    LOGGER.info(
                        "Downloaded Toronto Application Form for %s; filename=%s; extracted_chars=%s",
                        base_url,
                        suggested_name or "unknown",
                        len(text),
                    )
                    return text
            except Exception as exc:
                LOGGER.info("Could not download/parse Toronto Application Form for %s: %s", base_url, exc)
                continue

        return ""

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
        return any(marker in page_text for marker in ("page not found", "404", "access denied", "forbidden"))

    def _extract_document_links(self, html_text: str, base_url: str) -> dict[str, str]:
        """Extract required links from the rendered Supporting Documentation table."""
        soup = BeautifulSoup(html_text or "", "html.parser")
        docs = {name: "" for name in self.REQUIRED_DOCUMENTS}

        def maybe_capture(label_text: str, href: str) -> None:
            text = normalize_key(label_text)
            href = normalize_key(href)
            if not text or not href:
                return

            lowered = f"{text} {href}".lower()
            full_url = urljoin(base_url, href)

            for name, patterns in self.REQUIRED_DOCUMENTS.items():
                if docs.get(name):
                    continue
                if any(re.search(pattern, lowered, flags=re.I) for pattern in patterns):
                    docs[name] = full_url

        def element_context(element: Any) -> str:
            pieces = [element.get_text(" ", strip=True)]
            for parent_name in ("tr", "li"):
                parent = element.find_parent(parent_name)
                if parent:
                    pieces.append(parent.get_text(" ", strip=True))
                    break
            return normalize_key(" ".join(dict.fromkeys(piece for piece in pieces if piece)))

        def url_candidates(element: Any) -> list[str]:
            candidates: list[str] = []

            def add_from_text(value: Any, *, allow_raw: bool) -> None:
                raw = html.unescape(str(value or "")).strip()
                if not raw:
                    return

                found = False
                for match in re.findall(r"https?://[^\s'\"<>\\)]+", raw, flags=re.I):
                    candidates.append(match)
                    found = True

                for match in re.findall(r"(?<![A-Za-z0-9])(/[^\s'\"<>\\)]+)", raw):
                    candidates.append(match)
                    found = True

                for match in re.findall(
                    r"['\"]([^'\"]*(?:download|document|attachment|file)[^'\"]*)['\"]",
                    raw,
                    flags=re.I,
                ):
                    candidates.append(match)
                    found = True

                if allow_raw and not found:
                    lowered = raw.lower()
                    if not lowered.startswith(("javascript:", "mailto:", "tel:", "#")):
                        if "/" in raw or "download" in lowered or "document" in lowered or "file" in lowered:
                            candidates.append(raw)

            nodes = [element]
            try:
                nodes.extend(element.find_all(True))
            except Exception:
                pass

            for node in nodes:
                for attr_name, attr_value in getattr(node, "attrs", {}).items():
                    attr_lower = attr_name.lower()
                    if attr_lower == "data-id":
                        classes = " ".join(getattr(node, "get", lambda *_: "")("class", []) or [])
                        context_text = element_context(element)
                        doc_name = self._document_name_for_text(context_text)
                        if doc_name and "downloadFile".lower() in classes.lower():
                            candidates.append(self._download_button_fallback_url(base_url, doc_name, str(attr_value)))
                    if attr_lower in {"href", "src"} or attr_lower.startswith("data"):
                        add_from_text(attr_value, allow_raw=True)
                    elif attr_lower == "onclick":
                        add_from_text(attr_value, allow_raw=False)

            seen: set[str] = set()
            unique: list[str] = []
            for candidate in candidates:
                candidate = normalize_key(candidate)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    unique.append(candidate)
            return unique

        # Prefer full table-row context. Toronto's Action button says "Download",
        # while the real document name is in the Reference File column.
        for row in soup.select("table tbody tr, table tr"):
            row_text = element_context(row)
            for href in url_candidates(row):
                maybe_capture(row_text, href)
        # Normal anchors. In the Supporting Documentation table, the anchor
        # text may be generic; the document name/type is often in sibling cells.
        for link in soup.find_all("a", href=True):
            maybe_capture(element_context(link), link.get("href"))

        # Fallback for links/buttons outside a normal table row.
        for element in soup.select("a[href], button, [onclick], [data-href], [data-url], [data-download-url], [data-file-url]"):
            visible_text = element_context(element)
            for href in url_candidates(element):
                maybe_capture(visible_text, href)

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

        try:
            response = self.http.get(application_form_url)
        except Exception as exc:
            LOGGER.info("Could not download Toronto application form %s: %s", application_form_url, exc)
            return contacts

        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or application_form_url.lower().split("?")[0].endswith(".pdf"):
            text = self._extract_pdf_text(response.content)
        else:
            text = BeautifulSoup(response.text or "", "html.parser").get_text("\n", strip=True)

        return self._parse_application_form_contacts(text)

    def _extract_pdf_text(self, pdf_bytes: bytes) -> str:
        text_parts: list[str] = []
        try:
            import fitz

            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            max_pages = min(2, len(doc))
            for page_index in range(max_pages):
                page = doc[page_index]
                text_parts.append(page.get_text("text"))
                try:
                    for widget in page.widgets() or []:
                        field_name = normalize_key(getattr(widget, "field_name", ""))
                        field_value = normalize_key(getattr(widget, "field_value", ""))
                        if field_name or field_value:
                            text_parts.append(f"{field_name}: {field_value}".strip(" :"))
                except Exception:
                    pass

            if len("\n".join(text_parts).strip()) < 200 and self.config.get("ocr_image_pdfs", True):
                try:
                    import pytesseract
                    from PIL import Image

                    for page_index in range(max_pages):
                        page = doc[page_index]
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                        image = Image.open(io.BytesIO(pix.tobytes("png")))
                        text_parts.append(pytesseract.image_to_string(image))
                except Exception as exc:
                    LOGGER.info("OCR fallback unavailable for application form PDF: %s", exc)
        except Exception as exc:
            LOGGER.info("Could not parse application form PDF: %s", exc)

        return "\n".join(part for part in text_parts if part)

    def _parse_application_form_contacts(self, text: str) -> dict[str, dict[str, str]]:
        clean = re.sub(r"[ \t]+", " ", text or "")
        clean = re.sub(r"\n+", "\n", clean)

        def section_between(start_pattern: str, end_pattern: str) -> str:
            start_match = re.search(start_pattern, clean, flags=re.I | re.S)
            if not start_match:
                return ""
            start = start_match.start()
            end_match = re.search(end_pattern, clean[start_match.end():], flags=re.I | re.S)
            end = start_match.end() + end_match.start() if end_match else len(clean)
            return clean[start:end]

        def first_email(segment: str) -> str:
            match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", segment, flags=re.I)
            return match.group(0) if match else ""

        def first_phone(segment: str) -> str:
            match = re.search(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", segment)
            return normalize_key(match.group(0)) if match else ""

        def lines(segment: str) -> list[str]:
            return [normalize_key(line) for line in segment.splitlines() if normalize_key(line)]

        def extract_name(segment: str, email: str, label_words: list[str]) -> str:
            for line in lines(segment):
                if email and email in line:
                    candidate = normalize_key(line.replace(email, ""))
                    candidate = re.sub(r"Owner E-?mail:?|Business E-?mail:?", "", candidate, flags=re.I).strip(" :;,-")
                    if candidate and not any(word.lower() in candidate.lower() for word in label_words):
                        return candidate
            skip = (
                "registered owner",
                "owner e-mail",
                "business address",
                "business telephone",
                "business fax",
                "applicant name",
                "business e-mail",
                "applicant is",
            )
            for line in lines(segment):
                if any(marker in line.lower() for marker in skip):
                    continue
                if re.search(r"[A-Za-z]", line):
                    return re.sub(r"\s+", " ", line).strip(" :;,-")
            return ""

        def extract_address(segment: str) -> str:
            seg_lines = lines(segment)
            for index, line in enumerate(seg_lines):
                if "business address" in line.lower():
                    for candidate in seg_lines[index + 1 : index + 4]:
                        if not any(marker in candidate.lower() for marker in ("business telephone", "business fax", "applicant name", "applicant is")):
                            return shorten(candidate, 250)
            return ""

        owner_section = section_between(r"Registered Owner\(s\)|Owners? of subject land", r"Applicant name|Applicant is|This section for Office Use")
        applicant_section = section_between(r"Applicant name", r"This section for Office Use|File No|Declaration")

        owner_email = first_email(owner_section)
        applicant_email = first_email(applicant_section)

        return {
            "land_owner": {
                "name": extract_name(owner_section, owner_email, ["owner e-mail"]),
                "phone": first_phone(owner_section),
                "email": owner_email,
                "address": extract_address(owner_section),
            },
            "applicant": {
                "name": extract_name(applicant_section, applicant_email, ["business e-mail"]),
                "phone": first_phone(applicant_section),
                "email": applicant_email,
                "address": extract_address(applicant_section),
            },
        }

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
        item.setdefault("document_links", {name: "" for name in self.REQUIRED_DOCUMENTS})
        item.setdefault("land_owner", {"name": "", "phone": "", "email": "", "address": ""})
        item.setdefault("applicant", {"name": "", "phone": "", "email": "", "address": ""})

        if item["link_status"] != "current" or not detail_url:
            return item

        try:
            final_url, html_text = self._fetch_rendered_page(detail_url)
            if self._looks_expired(html_text):
                item["link_status"] = "expired"
                return item

            item["detail_url"] = final_url or detail_url
            item["document_links"] = self._extract_document_links(html_text, item["detail_url"])

            form_contacts = self._extract_application_form_contacts_from_rendered_html(html_text)
            if self._contacts_have_content(form_contacts):
                item.update(form_contacts)
            else:
                form_url = item["document_links"].get("Application Form")
                if form_url:
                    fallback_contacts = self._extract_application_form_contacts(form_url)
                    if self._contacts_have_content(fallback_contacts):
                        item.update(fallback_contacts)
        except Exception as exc:
            LOGGER.info("Could not enrich Toronto application page %s: %s", detail_url, exc)

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
            f"Description: {p.get('description') or 'Not found'}",
            f"Land Owner: {self._format_party(p.get('land_owner'))}",
            f"Applicant: {self._format_party(p.get('applicant'))}",
        ]
        lines.extend(self._format_document_links(p.get("document_links") or {}))
        return lines

    def _render_toronto_simplified(self, p: dict[str, Any]) -> list[str]:
        lines = [
            f"Addresses: {self._format_addresses_with_map(p)}",
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
            lines.append(f"  - {name}: {document_links.get(name) or 'Not found'}")
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
    raw_url = normalized_key_part(app.get("raw_application_url") or app.get("detail_url"))

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
        LOGGER.info("Checking Toronto Open Data development applications")
        try:
            toronto_monitor = TorontoOpenDataMonitor(http, config["toronto"])
            apps = toronto_monitor.fetch_new_candidates()
            toronto_items = to_notification_items_from_toronto(apps)

            # Performance-critical: do not render/enrich every recent Toronto
            # candidate on every run. Rendering the JavaScript application page
            # is expensive, and most candidates have already been seen. Filter
            # against SQLite first, then enrich only items that will actually be
            # considered for notification. On first run with notify_on_first_run
            # disabled, this marks candidates seen without opening hundreds of
            # detail pages.
            toronto_unseen = filter_unseen(
                store,
                toronto_items,
                bool(config.get("notify_on_first_run", False)),
                mark=not dry_run,
            )

            for item in toronto_unseen:
                enriched_payload = toronto_monitor.enrich_application(item.payload)
                ready_items.append(
                    dataclasses.replace(
                        item,
                        url=enriched_payload.get("detail_url")
                        or enriched_payload.get("raw_application_url")
                        or item.url,
                        payload=enriched_payload,
                    )
                )

            LOGGER.info(
                "Toronto Open Data yielded %d candidate application(s); %d new item(s) required enrichment",
                len(apps),
                len(toronto_unseen),
            )
        except Exception as exc:
            LOGGER.exception("Toronto Open Data check failed: %s", exc)

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor Toronto/Ottawa development applications and construction/development news.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file")
    parser.add_argument("--dry-run", action="store_true", help="Print notifications instead of sending them")
    args = parser.parse_args(argv)
    config_path = args.config if os.path.exists(args.config) else None
    if args.config and not config_path:
        print(f"Config {args.config!r} not found; using defaults.", file=sys.stderr)
    return run(load_config(config_path), dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
