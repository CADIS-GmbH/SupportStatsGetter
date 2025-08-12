import sys
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import text

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

