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
import textwrap
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from typing import Any, Iterable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

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
    """Monitor Toronto Development Applications through the current Open Data CKAN feed.

    This deliberately does not scrape the retired AIC detail endpoint. The daily
    Open Data CSV/API already contains the dates, status fields, addresses and
    public application links needed for monitoring.
    """

    FALLBACK_CSV_URL = (
        "https://ckan0.cf.opendata.inter.prod-toronto.ca/"
        "dataset/0aa7e480-9b48-4919-98e0-6af7615b7809/"
        "resource/77f8a66a-bd43-40e6-b6c9-12a2b03a5032/"
        "download/development-applications.csv"
    )
    LEGACY_AIC_PATTERNS = (
        "app.toronto.ca/AIC/",
        "secure.toronto.ca/AIC/",
        "app.toronto.ca/AIC/index.do",
        "secure.toronto.ca/AIC/index.do",
    )

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
            
            max_candidates = int(
                self.config.get("max_candidates", self.config.get("max_records", 0)) or 0
            )
            
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

        Do not apply max_records before filtering. The Toronto CSV is not sorted
        by submitted date, so slicing the first N raw rows can miss recent
        applications that appear much later in the file. Use raw_record_limit
        only as an emergency safety valve; cap notifications with
        max_candidates after filtering/grouping instead.
        """
        resources: list[dict[str, Any]] = []
        raw_record_limit = int(self.config.get("raw_record_limit", 0) or 0)
        try:
            package = self.http.get(f"{self.ckan_base}/package_show", params={"id": self.package_id}).json()
            if not package.get("success"):
                raise RuntimeError(f"CKAN package_show failed for {self.package_id}: {package}")
            resources = package.get("result", {}).get("resources", []) or []
        except Exception as exc:
            LOGGER.warning("Could not resolve Toronto CKAN package metadata; will try fallback CSV: %s", exc)

        urls_to_try: list[tuple[str, str, str]] = []
        chosen = self._choose_csv_resource(resources)
        if chosen and chosen.get("url"):
            urls_to_try.append((str(chosen["url"]), str(chosen.get("format", "csv")).lower(), str(chosen.get("name") or chosen.get("id") or "CSV resource")))

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
        """Match Toronto application type codes and long descriptions.

        Toronto Open Data can expose values such as ``OZ`` or longer labels that
        contain the code/description. Treat configured codes as prefixes or
        contained values instead of requiring exact string equality.
        """
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
            # Looser contains match for CKAN fields with prefixes/suffixes.
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
        detail_url = self._canonical_toronto_application_url(raw_detail_url)

        item = {
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
        return item
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
    
            link_info = self._validate_group_link(representative.get("raw_application_url"))
    
            app.update(
                {
                    "detail_url": link_info.get("detail_url"),
                    "link_status": link_info.get("link_status"),
                    "page_title": link_info.get("page_title"),
                }
            )
    
            page_fields = link_info.get("page_fields") or {}
            for field in (
                "file_number",
                "application_type",
                "status",
                "submitted_date",
                "ward",
                "description",
                "community_meeting_date",
            ):
                if page_fields.get(field):
                    app[field] = page_fields[field]
    
            applications.append(app)
    
        return applications
    
    
    def _application_group_key(self, row: dict[str, Any]) -> str:
        raw_url = normalized_key_part(row.get("raw_application_url"))
        if raw_url:
            return "url:" + raw_url
    
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
            url = normalize_key(row.get("raw_application_url"))
            has_current_link = int(bool(self._canonical_toronto_application_url(url)))
            has_description = int(bool(row.get("description")))
            date_value = parse_dt(row.get("submitted_date") or row.get("last_updated")) or datetime.min.replace(tzinfo=timezone.utc)
            return (has_current_link, has_description, date_value)
    
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
    def _validate_group_link(self, raw_url: str | None) -> dict[str, Any]:
        raw_url = normalize_key(raw_url)
        if not raw_url:
            return {"link_status": "void", "detail_url": None, "page_fields": {}}

        detail_url = self._canonical_toronto_application_url(raw_url)
        if not detail_url:
            return {"link_status": "expired", "detail_url": raw_url, "page_fields": {}}

        try:
            response = self.http.get(detail_url)
        except Exception as exc:
            LOGGER.info("Toronto application link is expired or unavailable: %s (%s)", detail_url, exc)
            return {"link_status": "expired", "detail_url": detail_url, "page_fields": {}}

        if not self._looks_like_toronto_application_page(detail_url, response.text):
            return {"link_status": "expired", "detail_url": detail_url, "page_fields": {}}

        return {
            "link_status": "current",
            "detail_url": detail_url,
            "page_title": self._extract_page_title(response.text),
            "page_fields": self._extract_application_page_fields(response.text),
        }

    def _looks_like_toronto_application_page(self, url: str, text: str) -> bool:
        if not self._canonical_toronto_application_url(url):
            return False
        soup = BeautifulSoup(text or "", "html.parser")
        page_text = soup.get_text(" ", strip=True).lower()
        if not page_text:
            return False
        if any(marker in page_text for marker in ("page not found", "404", "access denied", "forbidden")):
            return False
        return any(
            marker in page_text
            for marker in (
                "application details",
                "application number",
                "application type",
                "community meeting",
                "date submitted",
            )
        )

    def _extract_page_title(self, html_text: str) -> str:
        soup = BeautifulSoup(html_text or "", "html.parser")
        heading = soup.find(["h1", "h2"])
        if heading:
            return normalize_key(heading.get_text(" ", strip=True))
        if soup.title:
            return normalize_key(soup.title.get_text(" ", strip=True))
        return ""

    def _extract_application_page_fields(self, html_text: str) -> dict[str, str]:
        soup = BeautifulSoup(html_text or "", "html.parser")
        text = soup.get_text(" ", strip=True)
        return {
            "file_number": self._extract_label_value(text, ["Application Number", "Application #", "File Number"]),
            "application_type": self._extract_label_value(text, ["Application Type", "Type"]),
            "status": self._extract_label_value(text, ["Status", "Application Status"]),
            "submitted_date": self._extract_label_value(text, ["Date Submitted", "Submitted", "Received Date"]),
            "ward": self._extract_label_value(text, ["Ward"]),
            "community_meeting_date": self._extract_label_value(text, ["Community Meeting Date", "Meeting Date"]),
            "description": shorten(self._extract_label_value(text, ["Description", "Proposal", "Application Description"]), 1200),
        }

    def _extract_label_value(self, text: str, labels: list[str]) -> str:
        if not text:
            return ""
        stop_labels = [
            "Application Number",
            "Application #",
            "File Number",
            "Application Type",
            "Type",
            "Status",
            "Application Status",
            "Date Submitted",
            "Submitted",
            "Received Date",
            "Ward",
            "Community Meeting Date",
            "Meeting Date",
            "Description",
            "Proposal",
            "Application Description",
            "Contact",
            "Documents",
        ]
        label_pattern = "|".join(re.escape(label) for label in labels)
        stop_pattern = "|".join(re.escape(label) for label in stop_labels)
        match = re.search(
            rf"(?:{label_pattern})\s*:?\s*(.+?)(?=\s+(?:{stop_pattern})\s*:?|$)",
            text,
            flags=re.I,
        )
        return normalize_key(match.group(1)) if match else ""

    def _link_status(self, raw_url: str, detail_url: str | None) -> str:
        if not normalize_key(raw_url):
            return "void"
        if detail_url:
            return "current"
        return "expired"

    def _is_legacy_toronto_url(self, url: str) -> bool:
        lowered = normalize_key(url).lower()
        return any(pattern.lower() in lowered for pattern in self.LEGACY_AIC_PATTERNS)

    def _canonical_toronto_application_url(self, url: str) -> str | None:
        url = normalize_key(url)
        if not url or self._is_legacy_toronto_url(url):
            return None
    
        if url.startswith("/"):
            url = urljoin("https://www.toronto.ca/", url)
    
        parsed = urlparse(url)
    
        if parsed.scheme not in {"http", "https"}:
            return None
        if "toronto.ca" not in parsed.netloc.lower():
            return None
        if "/application-details/" not in parsed.path:
            return None
    
        params = parse_qs(parsed.query)
        if not all(params.get(key, [""])[0] for key in ("id", "pid", "title")):
            return None
    
        return url

    def _construct_detail_url(self, record: dict[str, Any], title: str) -> str | None:
        """Build the current public Toronto application-details URL when ids exist.

        Expected public format:
        https://www.toronto.ca/city-government/planning-development/application-details/?id=<id>&pid=<pid>&title=<TITLE>
        """
        keymap = {compact_key(k): k for k in record.keys()}

        def value_for(names: list[str]) -> str | None:
            for name in names:
                c = compact_key(name)
                if c in keymap:
                    original = keymap[c]
                    # Avoid CKAN datastore row id. That is not the Toronto application id.
                    if original == "_id":
                        continue
                    value = record.get(original)
                    if value not in (None, ""):
                        return normalize_key(value)
            return None

        app_id = value_for([
            "APPLICATION_ID",
            "APPLICATIONID",
            "APP_ID",
            "APPID",
            "FOLDER_ID",
            "FOLDERID",
            "FOLDER_RSN",
            "FOLDERRSN",
            "ID",
        ])

        pid = value_for([
            "PID",
            "PROPERTY_ID",
            "PROPERTYID",
            "PROP_ID",
            "PROPID",
            "PARCEL_ID",
            "PARCELID",
        ])

        title_value = (
            value_for(["TITLE", "APPLICATION_TITLE", "ADDRESS", "LOCATION"])
            or title
            or "APPLICATION"
        )

        if not app_id or not pid:
            return None

        slug = re.sub(r"[^A-Za-z0-9]+", "-", str(title_value).upper()).strip("-")
        if not slug:
            slug = "APPLICATION"

        return (
            "https://www.toronto.ca/city-government/planning-development/"
            f"application-details/?id={quote_plus(app_id)}&pid={quote_plus(pid)}&title={quote_plus(slug)}"
        )

    def enrich_application(self, item: dict[str, Any]) -> dict[str, Any]:
        """No-op enrichment for Toronto.

        The previous implementation fetched AIC/detail pages to classify supporting
        documents. That path is intentionally disabled because the current monitor
        should rely on Toronto Open Data rows and their APPLICATION_URL values only.
        """
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
        meeting_bits = [
            p.get("community_meeting_date") or "",
            p.get("community_meeting_time") or "",
            p.get("community_meeting_location") or "",
        ]
        meeting = " | ".join(bit for bit in meeting_bits if bit) or "Not found"
        contact_bits = [p.get("contact_name") or "", p.get("contact_phone") or "", p.get("contact_email") or ""]
        contact = " | ".join(bit for bit in contact_bits if bit) or "Not found"
        ward = " ".join(bit for bit in [p.get("ward_number") or "", p.get("ward") or ""] if bit) or "Not found"
        addresses = p.get("addresses") or ([p.get("address")] if p.get("address") else [])
        address_text = "; ".join(addresses) or "Not found"
        csv_rows = ", ".join(p.get("csv_row_ids") or []) or "Not found"
        file_numbers = ", ".join(p.get("file_numbers") or []) or p.get("file_number") or "Not found"
        return [
            f"File number(s): {file_numbers}",
            f"Addresses: {address_text}",
            f"Metadata rows: {p.get('metadata_row_count') or 1} ({csv_rows})",
            f"Type/status: {p.get('application_type') or 'Not found'} / {p.get('status') or 'Not found'}",
            f"Submitted: {p.get('submitted_date') or 'Not found'}",
            f"Ward: {ward}",
            f"Community meeting: {meeting}",
            f"Contact: {contact}",
            f"Link status: {p.get('link_status') or 'Not found'}",
            f"Application page title: {p.get('page_title') or 'Not found'}",
            f"Description: {p.get('description') or 'Not found'}",
        ]

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
                url=app.get("detail_url") or app.get("raw_application_url") or None
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

    if config.get("toronto", {}).get("enabled", True):
        LOGGER.info("Checking Toronto Open Data development applications")
        try:
            toronto_monitor = TorontoOpenDataMonitor(http, config["toronto"])
            apps = toronto_monitor.fetch_new_candidates()
            enriched_apps = [toronto_monitor.enrich_application(app) for app in apps]
            candidate_items.extend(to_notification_items_from_toronto(enriched_apps))
            LOGGER.info("Toronto Open Data yielded %d candidate application(s)", len(enriched_apps))
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

    LOGGER.info("All sources yielded %d candidate item(s): %s", len(candidate_items), summarize_sources(candidate_items))
    unseen = filter_unseen(store, candidate_items, bool(config.get("notify_on_first_run", False)), mark=not dry_run)
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
