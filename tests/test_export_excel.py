import datetime
import sys
from pathlib import Path

from openpyxl import load_workbook, Workbook
from sqlalchemy import text
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from SupportStatsGetter import Config, SupportStatsGetter


def test_export_views_writes_numbers(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    with getter.store.engine.begin() as conn:
        conn.execute(text("CREATE VIEW test_view AS SELECT 42 AS value"))
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    ws = wb["test_view"]
    cell_value = ws["A2"].value
    assert cell_value == 42
    assert isinstance(cell_value, (int, float))


def test_export_views_keeps_cw_strings(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    with getter.store.engine.begin() as conn:
        conn.execute(text("CREATE VIEW test_view_cw AS SELECT '2025.12' AS cw"))
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    ws = wb["test_view_cw"]
    cell_value = ws["A2"].value
    assert cell_value == "2025.12"
    assert isinstance(cell_value, str)


def test_export_views_escapes_formulas(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    with getter.store.engine.begin() as conn:
        conn.execute(text("CREATE VIEW test_view_formula AS SELECT '=2+2' AS val"))
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    ws = wb["test_view_formula"]
    cell = ws["A2"]
    assert cell.value == "'=2+2"  # prefixed with apostrophe
    assert cell.data_type == "s"


def test_reporting_weeks_view_exported(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    assert "reporting_weeks" in wb.sheetnames
    ws = wb["reporting_weeks"]
    assert ws["A2"].value == "2025.01"
    assert ws["B2"].value == "2025.01"
    assert ws["C2"].value == "2025.Q1"


def test_export_overwrite_with_backup(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    with getter.store.engine.begin() as conn:
        conn.execute(text("CREATE VIEW test_view AS SELECT 1 AS value"))
    output = tmp_path / "out.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "old_sheet"
    ws["A1"] = "old"
    wb.save(output)

    getter.export_views_to_excel(output, mode="overwrite_with_backup")

    backups = list(tmp_path.glob("out_*.xlsx"))
    assert backups

    wb2 = load_workbook(output)
    assert "old_sheet" not in wb2.sheetnames
    assert wb2["test_view"]["A2"].value == 1


def test_export_no_overwrite(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    with getter.store.engine.begin() as conn:
        conn.execute(text("CREATE VIEW test_view AS SELECT 1 AS value"))
    output = tmp_path / "out.xlsx"
    output.write_bytes(b"")

    with pytest.raises(FileExistsError):
        getter.export_views_to_excel(output, mode="no_overwrite")


def test_dashboard_is_first_sheet_with_controls(tmp_path):
    start = f"{datetime.date.today().year}.Q1"
    config = Config(db_path=tmp_path / "test.db", dashboard_start_quarter=start)
    getter = SupportStatsGetter(config)
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    assert wb.sheetnames[0] == "Dashboard"
    assert wb["dashboard_calc"].sheet_state == "hidden"
    ws = wb["Dashboard"]
    assert ws["C4"].value == start
    assert ws["C5"].value == "Week"
    assert ws["C6"].value == "No"
    assert "Dash_period" in ws.defined_names
    assert "Dash_new_tickets" in ws.defined_names
    assert len(ws._charts) == 10


def test_dashboard_start_quarter_defaults_to_one_year_back(tmp_path):
    config = Config(db_path=tmp_path / "test.db", dashboard_start_quarter="1999.Q1")
    getter = SupportStatsGetter(config)
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    calc = wb["dashboard_calc"]
    quarters = [c.value for c in calc["D"][1:] if c.value]
    now_quarter = calc["B3"].value
    expected = quarters[max(0, quarters.index(now_quarter) - 4)]
    assert wb["Dashboard"]["C4"].value == expected


def _ticket(ticket_id, request_type, organization):
    return {
        "id": ticket_id,
        "key": f"CSM-{ticket_id}",
        "status": "Open",
        "issuetype": "Support",
        "request_type": request_type,
        "created": datetime.datetime(2025, 2, 3),
        "created_week": "2025.06",
        "primary_organization": organization,
    }


def test_dashboard_customer_charts_per_category(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    getter.store.insert_many(
        getter.store.jira_tickets,
        [_ticket("1", "Hardware issue", "ACME"), _ticket("2", "Bug", "ACME")],
    )
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    ws = wb["customer_weekly_summary"]
    rows = [[c.value for c in row][:6] for row in ws.iter_rows(min_row=2)]
    assert rows == [
        ["2025.06", "2025.02", "2025.Q1", "ACME", "HW", 1],
        ["2025.06", "2025.02", "2025.Q1", "ACME", "SW", 1],
    ]
    calc = wb["dashboard_calc"]
    assert calc["A9"].value == "last_full_period"
    assert calc["F2"].value == "ACME"
    # 10 period charts + new (all, HW, SW) and hours per customer
    dash = wb["Dashboard"]
    assert len(dash._charts) == 14
    assert "Dash_cust0_org" in dash.defined_names


def test_export_sanitizes_sheet_and_values(tmp_path):
    config = Config(db_path=tmp_path / "test.db")
    getter = SupportStatsGetter(config)
    with getter.store.engine.begin() as conn:
        conn.exec_driver_sql('CREATE VIEW "bad/?*[]:name" AS SELECT "bad" || char(11) || "value" AS val')
    output = tmp_path / "out.xlsx"
    getter.export_views_to_excel(output)
    wb = load_workbook(output)
    assert "bad______name" in wb.sheetnames
    ws = wb["bad______name"]
    assert ws["A2"].value == "badvalue"

