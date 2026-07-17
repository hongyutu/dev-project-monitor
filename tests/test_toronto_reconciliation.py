from __future__ import annotations

import copy

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
