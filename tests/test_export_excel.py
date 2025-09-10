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

