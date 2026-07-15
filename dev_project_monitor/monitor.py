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
from urllib.parse import urljoin, quote_plus

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
        "max_records": 500,
        "lookback_days": 45,
        "application_types": [],
    },
    "ottawa": {
        "enabled": True,
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


class TorontoAICMonitor:
    def __init__(self, http: HttpClient, config: dict[str, Any]) -> None:
        self.http = http
        self.config = config
        self.ckan_base = config["ckan_base_url"].rstrip("/")
        self.package_id = config.get("package_id", "development-applications")

    def fetch_new_candidates(self) -> list[dict[str, Any]]:
        records = self._fetch_ckan_records()
        lookback_days = int(self.config.get("lookback_days", 45))
        cutoff = utcnow() - timedelta(days=lookback_days)
        normalized: list[dict[str, Any]] = []
        allowed_types = {compact_key(x) for x in self.config.get("application_types", []) if x}

        for record in records:
            item = self._normalize_record(record)
            if not item.get("file_number") and not item.get("address"):
                continue
            actual_type = compact_key(item.get("application_type"))
            if allowed_types and actual_type not in allowed_types:
                continue
            date_value = parse_dt(item.get("submitted_date") or item.get("last_updated"))
            if date_value and date_value < cutoff:
                continue
            normalized.append(item)
        return normalized

    def _fetch_ckan_records(self) -> list[dict[str, Any]]:
        package = self.http.get(f"{self.ckan_base}/package_show", params={"id": self.package_id}).json()
        if not package.get("success"):
            raise RuntimeError(f"CKAN package_show failed for {self.package_id}: {package}")
        resources = package.get("result", {}).get("resources", [])
        max_records = int(self.config.get("max_records", 500))

        datastore_resources = [r for r in resources if r.get("datastore_active")]
        for resource in datastore_resources:
            try:
                return self._fetch_datastore_records(resource["id"], max_records=max_records)
            except Exception as exc:
                LOGGER.warning("Could not read datastore resource %s: %s", resource.get("id"), exc)

        downloadable = [r for r in resources if str(r.get("format", "")).lower() in {"csv", "json", "geojson"} and r.get("url")]
        for resource in downloadable:
            try:
                return self._fetch_resource_url(resource["url"], str(resource.get("format", "")).lower())[:max_records]
            except Exception as exc:
                LOGGER.warning("Could not read resource %s: %s", resource.get("url"), exc)

        raise RuntimeError("No usable CKAN datastore or CSV/JSON/GeoJSON resource found for development-applications")

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
        text = response.text
        if fmt == "csv" or url.lower().endswith(".csv"):
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
        # Prefer the current public City of Toronto application-details URL.
        # The older APPLICATION_URL / app.toronto.ca/AIC/index.do?folderRsn=...
        # often redirects to secure.toronto.ca and returns 403 from GitHub Actions.
        legacy_detail_url = normalize_key(pick("detail_url"))
        if legacy_detail_url and not legacy_detail_url.startswith("http"):
            legacy_detail_url = urljoin("https://www.toronto.ca/", legacy_detail_url)

        detail_url = self._construct_detail_url(record, address) or legacy_detail_url

        item = {
            "file_number": file_number,
            "address": address,
            "description": shorten(normalize_key(pick("description")), 1200),
            "application_type": normalize_key(pick("application_type")),
            "status": normalize_key(pick("status")),
            "submitted_date": normalize_key(pick("submitted_date")),
            "last_updated": normalize_key(pick("last_updated")),
            "detail_url": detail_url,
            "ward": normalize_key(pick("ward")),
            "district": normalize_key(pick("district")),
            "raw": record,
        }
        return item

    def _construct_detail_url(self, record: dict[str, Any], title: str) -> str | None:
        """Build current public Toronto application-details URL.

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
        detail_url = item.get("detail_url")
        docs = {category: [] for category in DOCUMENT_PATTERNS}
        page_summary = ""
        if detail_url:
            try:
                html_text = self.http.get(detail_url).text
                docs = self._extract_supporting_documents(html_text, detail_url)
                page_summary = self._extract_page_summary(html_text)
            except Exception as exc:
                LOGGER.warning("Could not fetch AIC detail page %s: %s", detail_url, exc)
        if not item.get("description") and page_summary:
            item["description"] = page_summary
        item["documents"] = docs
        return item

    def _extract_supporting_documents(self, html_text: str, base_url: str) -> dict[str, list[dict[str, str]]]:
        soup = BeautifulSoup(html_text, "html.parser")
        links: list[tuple[str, str]] = []
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a.get("href", ""))
            label = shorten(a.get_text(" ", strip=True) or href, 300)
            links.append((label, href))

        # Some AIC pages store URLs inside JSON payloads instead of normal anchors.
        for match in re.finditer(r"https?:\\?/\\?/[^\"'<>\s]+", html_text):
            raw = match.group(0).replace("\\/", "/")
            label = raw.rsplit("/", 1)[-1]
            links.append((label, raw))

        docs: dict[str, list[dict[str, str]]] = {category: [] for category in DOCUMENT_PATTERNS}
        seen: set[tuple[str, str]] = set()
        for label, href in links:
            haystack = f"{label} {href}".lower()
            for category, patterns in DOCUMENT_PATTERNS.items():
                if any(re.search(pattern, haystack, re.I) for pattern in patterns):
                    key = (category, href)
                    if key not in seen:
                        seen.add(key)
                        docs[category].append({"label": label, "url": href})
        return docs

    def _extract_page_summary(self, html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text(" ", strip=True)
        match = re.search(r"(Proposal|Description|Application Description)\s*[:\-]?\s*(.{80,1200})", text, re.I)
        return shorten(match.group(2), 800) if match else ""



class OttawaDevAppsMonitor:
    """Monitor City of Ottawa DevApps export data.

    The public DevApps interface is JavaScript-rendered, so this monitor uses the
    linked REST export endpoint instead of trying to scrape the search page.
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

        # Fallback for whitespace-rendered exports. This is not the primary path,
        # but it prevents a hard failure if the endpoint changes its delimiters.
        return []

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
            "snippet": shorten(clean, 700),
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
        subject = f"Development project monitor: {len(items)} new item(s)"
        text_body = self._render_text(items)
        json_body = [dataclasses.asdict(item) for item in items]
        if self.dry_run:
            print(text_body)
            return
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

    def _render_text(self, items: list[NotificationItem]) -> str:
        sections = ["Development Project Monitor", f"Generated: {utcnow().isoformat()}", ""]
        for idx, item in enumerate(items, start=1):
            p = item.payload
            sections.append(f"{idx}. [{item.kind}] {item.title}")
            if item.url:
                sections.append(f"URL: {item.url}")
            if item.kind == "toronto_aic":
                sections.extend(self._render_toronto(p))
            elif item.kind == "ottawa_devapps":
                sections.extend(self._render_ottawa(p))
            else:
                sections.extend(self._render_news(p))
            sections.append("-" * 72)
        return "\n".join(sections)

    def _render_toronto(self, p: dict[str, Any]) -> list[str]:
        lines = [
            f"File number: {p.get('file_number') or 'Not found'}",
            f"Address: {p.get('address') or 'Not found'}",
            f"Type/status: {p.get('application_type') or 'Not found'} / {p.get('status') or 'Not found'}",
            f"Submitted: {p.get('submitted_date') or 'Not found'}",
            f"Description: {p.get('description') or 'Not found'}",
            "Supporting documents:",
        ]
        docs = p.get("documents") or {}
        labels = {
            "application_form": "Application form",
            "civil_site_plan": "Civil / site plan / servicing",
            "architectural": "Architectural plans",
            "structural": "Structural plans",
            "geotechnical": "Geotechnical report",
            "hydrogeological": "Hydrogeological report",
        }
        for key, label in labels.items():
            matches = docs.get(key) or []
            if not matches:
                lines.append(f"  - {label}: not found on detail page")
            else:
                for match in matches[:5]:
                    lines.append(f"  - {label}: {match.get('label')} | {match.get('url')}")
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
            f"Snippet: {extracted.get('snippet') or p.get('summary') or 'Not found'}",
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


def to_notification_items_from_toronto(apps: Iterable[dict[str, Any]]) -> list[NotificationItem]:
    items: list[NotificationItem] = []
    for app in apps:
        key = compact_key(app.get("file_number")) or stable_hash({"address": app.get("address"), "submitted": app.get("submitted_date")})
        title = app.get("address") or app.get("file_number") or "Toronto development application"
        items.append(
            NotificationItem(
                source="toronto_aic",
                item_key=key,
                title=title,
                url=app.get("detail_url"),
                kind="toronto_aic",
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
        LOGGER.info("Checking Toronto AIC development applications")
        try:
            toronto_monitor = TorontoAICMonitor(http, config["toronto"])
            apps = toronto_monitor.fetch_new_candidates()
            enriched_apps = [toronto_monitor.enrich_application(app) for app in apps]
            candidate_items.extend(to_notification_items_from_toronto(enriched_apps))
            LOGGER.info("Toronto AIC yielded %d candidate application(s)", len(enriched_apps))
        except Exception as exc:
            LOGGER.exception("Toronto AIC check failed: %s", exc)

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

    unseen = filter_unseen(store, candidate_items, bool(config.get("notify_on_first_run", False)), mark=not dry_run)
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
