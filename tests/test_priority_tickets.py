import datetime
import sys
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter


def add_ticket(getter, key, priority, changes, resolved=None):
    created = datetime.datetime(2026, 10, 5, 9, 0)  # Monday, week 41
    getter.store.insert_many(getter.store.jira_tickets, [{
        "id": key, "key": key, "summary": f"Summary {key}", "status": "Resolved" if resolved else "Open",
        "priority": priority, "created": created, "updated": created, "resolved": resolved,
        "issuetype": "Support", "primary_organization": "ACME",
    }])
    getter.store.insert_many(getter.store.jira_changelog, [
        {
            "id": f"{key}-{i}", "history_id": f"{key}-{i}", "ticket_id": key, "author": "Agent",
            "created": when, "field": field, "from_string": frm, "to_string": to,
        }
        for i, (when, field, frm, to) in enumerate(changes)
    ])
    getter.recalculate_tickets([key])


def test_list_has_the_tickets_behind_the_priority_kpis(tmp_path):
    getter = SupportStatsGetter(Config(
        db_path=tmp_path / "test.db", jira_base_url="https://example.atlassian.net"
    ))
    resolved = datetime.datetime(2026, 10, 16, 9, 0)  # Friday, week 42
    # raised to High on Tuesday of week 41, resolved in week 42
    add_ticket(getter, "SUP-1", "High", [
        (datetime.datetime(2026, 10, 6, 10, 0), "priority", "Medium", "High"),
        (resolved, "status", "Open", "Resolved"),
    ], resolved=resolved)
    # always Medium: not listed
    add_ticket(getter, "SUP-2", "Medium", [
        (resolved, "status", "Open", "Resolved"),
    ], resolved=resolved)

    with getter.store.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT week, ticket_key, high_priority, escalated, escalated_on, week_priority, summary "
            "FROM priority_tickets WHERE week IN ('2026.41', '2026.42') ORDER BY week"
        )).all()
        kpis = dict(conn.execute(text(
            "SELECT week, high_or_critical_tickets + 100 * escalated_tickets "
            "FROM weekly_support_summary WHERE week IN ('2026.41', '2026.42')"
        )).all())
    assert [tuple(r) for r in rows] == [
        ("2026.41", "SUP-1", 1, 1, "2026-10-06", "High", "Summary SUP-1"),
        ("2026.42", "SUP-1", 1, 0, None, "High", "Summary SUP-1"),
    ]
    # the same tickets the summary counts
    assert kpis == {"2026.41": 101, "2026.42": 1}

    out = tmp_path / "out.xlsx"
    getter.export_views_to_excel(out, "overwrite")
    wb = load_workbook(out)
    assert wb.sheetnames[:2] == ["Dashboard", "priority_tickets"]
    ws = wb["priority_tickets"]
    assert ws.freeze_panes == "A2"
    key_col = [c.value for c in ws[1]].index("ticket_key") + 1
    link = ws.cell(row=2, column=key_col).hyperlink
    assert link is not None and link.target == "https://example.atlassian.net/browse/SUP-1"
