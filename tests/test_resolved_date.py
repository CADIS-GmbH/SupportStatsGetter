import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import calculate_kpi_fields


def ts(day):
    return datetime.datetime(2026, 10, day, 12, 0)


def change(day, frm, to):
    return {"created": ts(day), "items": [{"field": "status", "fromString": frm, "toString": to}]}


def issue(status, jira_resolved=None):
    return {"id": "1", "created": ts(1), "updated": ts(27), "resolved": jira_resolved,
            "fields": {"status": {"name": status}}}


def test_auto_close_does_not_move_the_resolved_date():
    changelog = [
        change(2, "Waiting for support", "Waiting for customer"),
        change(6, "Waiting for customer", "Resolved"),
        change(27, "Resolved", "Closed"),
    ]
    # Jira sets resolutiondate only on the auto-close
    result = calculate_kpi_fields(issue("Closed", jira_resolved=ts(27)), [], changelog)
    assert result["resolved"] == ts(6)
    assert result["ResolvedWeek"] == "2026.41"


def test_reopened_ticket_gets_its_last_closing():
    changelog = [
        change(6, "Waiting for customer", "Resolved"),
        change(7, "Resolved", "Waiting for support"),
        change(20, "Waiting for support", "Resolved"),
    ]
    assert calculate_kpi_fields(issue("Resolved"), [], changelog)["resolved"] == ts(20)


def test_reopened_open_ticket_has_no_resolved_date():
    changelog = [
        change(6, "Waiting for customer", "Resolved"),
        change(7, "Resolved", "Waiting for support"),
    ]
    result = calculate_kpi_fields(issue("Waiting for support"), [], changelog)
    assert result["resolved"] is None
    assert result["ResolvedWeek"] is None


def test_jira_date_without_status_history():
    assert calculate_kpi_fields(issue("Closed", jira_resolved=ts(9)), [], [])["resolved"] == ts(9)
