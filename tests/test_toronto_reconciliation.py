from __future__ import annotations

import copy
from pathlib import Path

from dev_project_monitor.monitor import (
    DEFAULT_CONFIG,
    HttpClient,
    Notifier,
    TorontoOpenDataMonitor,
)


def make_monitor() -> TorontoOpenDataMonitor:
    config = copy.deepcopy(DEFAULT_CONFIG["toronto"])
    return TorontoOpenDataMonitor(HttpClient("test-agent", 5), config)


def test_rejects_legacy_aic_folder_and_application_page_as_documents() -> None:
    monitor = make_monitor()
    base = (
        "https://www.toronto.ca/city-government/planning-development/"
        "application-details/?id=5870111&pid=96304&title=130%20FUNDY%20BAY%20BLVD"
    )
    assert not monitor._is_meaningful_document_url(
        "http://app.toronto.ca/AIC/index.do?folderRsn=OfX5rPIspmJMJzRfV5%2BXtw%3D%3D",
        base,
    )
    assert not monitor._is_meaningful_document_url(base + "#supporting-documentation", base)
    assert monitor._is_meaningful_document_url(
        "https://www.toronto.ca/files/application-form.pdf", base
    )


def test_fixture_preserves_row_availability_without_promoting_aic_url() -> None:
    monitor = make_monitor()
    base = (
        "https://www.toronto.ca/city-government/planning-development/"
        "application-details/?id=5870111&pid=96304&title=130%20FUNDY%20BAY%20BLVD"
    )
    fixture = monitor._download_links_fixture_html(
        {}, {"Application Form", "Civil and Utilities Plans"}, base
    )
    fixture += (
        '<table><tr><td>Civil and Utilities Plans</td><td>'
        '<a href="http://app.toronto.ca/AIC/index.do?folderRsn=bad">Download</a>'
        "</td></tr></table>"
    )
    docs = monitor._extract_document_links(fixture, base)
    assert docs["Application Form"].startswith(base + "#")
    assert docs["Civil and Utilities Plans"].startswith(base + "#")
    assert "app.toronto.ca/AIC" not in docs["Civil and Utilities Plans"]


def test_application_service_state_requires_real_application_content() -> None:
    monitor = make_monitor()

    class Locator:
        def __init__(self, value: str) -> None:
            self.value = value

        def inner_text(self, timeout: int = 0) -> str:
            return self.value

    class Scope:
        def __init__(self, value: str) -> None:
            self.value = value

        def locator(self, _selector: str) -> Locator:
            return Locator(self.value)

    monitor._document_scopes = lambda _page: [
        Scope("Loading...")
    ]
    assert monitor._application_service_state(object()) == "loading"

    monitor._document_scopes = lambda _page: [
        Scope("Supporting Documentation Application Status Milestone Status")
    ]
    assert monitor._application_service_state(object()) == "ready"

    monitor._document_scopes = lambda _page: [
        Scope("We are currently performing maintenance. Try again later.")
    ]
    assert monitor._application_service_state(object()) == "maintenance"


def test_notifier_labels_application_page_as_availability_not_direct_file() -> None:
    notifier = Notifier({"notifications": {}}, dry_run=True)
    base = (
        "https://www.toronto.ca/city-government/planning-development/"
        "application-details/?id=5870111&pid=96304&title=130%20FUNDY%20BAY%20BLVD"
    )
    lines = notifier._format_document_links(
        {"Application Form": base + "#supporting-documentation"}
    )
    rendered = "\n".join(lines)
    assert "Available — open Supporting Documentation" in rendered
    assert "Application Form: Not found" not in rendered


def test_defaults_disable_partial_failure_notifications_and_api_replay() -> None:
    toronto = DEFAULT_CONFIG["toronto"]
    assert toronto["browser_channel"] == "chromium"
    assert toronto["report_partial_on_enrichment_failure"] is False
    assert "document_api_replay_timeout_ms" not in toronto
    assert "application_service_ready_confirmations" not in toronto


def test_notification_key_prefers_canonical_url_over_poisoned_legacy_aic_key() -> None:
    from dev_project_monitor.monitor import stable_hash, normalized_key_part, toronto_notification_key

    legacy = (
        "http://app.toronto.ca/AIC/index.do?"
        "folderRsn=OfX5rPIspmJMJzRfV5%2BXtw%3D%3D"
    )
    canonical = (
        "https://www.toronto.ca/city-government/planning-development/"
        "application-details/?folderRsn=OfX5rPIspmJMJzRfV5%2BXtw%3D%3D"
    )
    result = toronto_notification_key(
        {"raw_application_url": legacy, "detail_url": canonical}
    )
    legacy_key = "toronto:" + stable_hash(
        {"application_url": normalized_key_part(legacy)}
    )[:40]
    canonical_key = "toronto:" + stable_hash(
        {"application_url": normalized_key_part(canonical)}
    )[:40]
    assert result == canonical_key
    assert result != legacy_key


def test_ready_application_scope_wins_over_stale_maintenance_frame() -> None:
    monitor = make_monitor()

    class Locator:
        def __init__(self, value: str) -> None:
            self.value = value

        def inner_text(self, timeout: int = 0) -> str:
            return self.value

    class Scope:
        def __init__(self, value: str) -> None:
            self.value = value

        def locator(self, _selector: str) -> Locator:
            return Locator(self.value)

        def evaluate(self, _script: str):
            return {
                "markers": {},
                "textLength": len(self.value),
                "loadingOnly": False,
            }

    monitor._document_scopes = lambda _page: [
        Scope("We are currently performing maintenance. Try again later."),
        Scope("Supporting Documentation Application Status Milestone Status"),
    ]
    assert monitor._application_service_state(object()) == "ready"


def test_first_strong_ready_poll_is_unconditionally_latched() -> None:
    monitor = make_monitor()
    monitor.config["application_service_retries"] = 1
    monitor.config["application_service_timeout_seconds"] = 15
    monitor.config["application_service_ready_confirmations"] = 2

    calls = {"count": 0}

    def state(_page):
        calls["count"] += 1
        if calls["count"] == 1:
            return "ready"
        raise AssertionError("readiness loop polled again after a strong ready result")

    monitor._application_service_state = state
    monitor._application_widget_probe = lambda _page: {
        "ready": False,
        "marker_count": 0,
        "strong_marker_count": 0,
    }

    class Mouse:
        def wheel(self, _x: int, _y: int) -> None:
            return None

    class Page:
        mouse = Mouse()

        def goto(self, *_args, **_kwargs) -> None:
            return None

        def reload(self, *_args, **_kwargs) -> None:
            return None

        def wait_for_timeout(self, _ms: int) -> None:
            return None

    assert monitor._open_and_wait_for_application(Page(), "https://example.invalid") == "ready"
    assert calls["count"] == 1


def test_ready_path_primes_supporting_documentation_before_secondary_probe() -> None:
    source = Path(__file__).parents[1] / "dev_project_monitor" / "monitor.py"
    text = source.read_text(encoding="utf-8")
    prime = text.index("prime_clicks = self._click_supporting_docs_with_playwright_locators(page)")
    probe = text.index('diagnostics["widget_probe"] = self._application_widget_probe(page)', prime)
    identity = text.index("self._log_toronto_browser_identity(page)", prime)
    assert prime < probe
    assert prime < identity


def test_mount_loop_clicks_first_supporting_text_before_any_scroll() -> None:
    monitor = make_monitor()
    monitor.config["supporting_docs_mount_wait_seconds"] = 2
    calls = {"probe": 0, "click": 0, "state": 0, "scroll": 0}

    def probe(_page):
        calls["probe"] += 1
        if calls["click"]:
            return {
                "downloadButtons": 1,
                "supportingTextMatches": 1,
                "referenceFileMatches": 1,
                "documentRows": 1,
            }
        return {
            "downloadButtons": 0,
            "supportingTextMatches": 1,
            "referenceFileMatches": 0,
            "documentRows": 0,
        }

    def state(_page):
        calls["state"] += 1
        return {
            "found": True,
            "expanded": bool(calls["click"]),
            "empty": False,
            "visible_rows": int(bool(calls["click"])),
            "open": bool(calls["click"]),
        }

    def click(_page):
        calls["click"] += 1
        return 1

    class Scope:
        def evaluate(self, _script):
            calls["scroll"] += 1
            raise AssertionError("mount loop scrolled after Supporting Documentation was detected")

    class Page:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monitor._supporting_docs_probe = probe
    monitor._supporting_documentation_state = state
    monitor._click_supporting_docs_with_playwright_locators = click
    monitor._document_scopes = lambda _page: [Scope()]

    result = monitor._scroll_until_supporting_docs_mounted(Page())
    assert calls["click"] == 1
    assert calls["scroll"] == 0
    assert result["documentRows"] == 1


def test_ci_uses_headful_bundled_chromium_under_xvfb() -> None:
    assert DEFAULT_CONFIG["toronto"]["headed"] is True
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "dev-project-monitor.yml").read_text(
        encoding="utf-8"
    )
    assert "xvfb-run -a" in workflow
    assert "headed: true" in (root / "config.yml").read_text(encoding="utf-8")
