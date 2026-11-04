"""取込用Excelの作成と出力履歴（要件定義書 F5）のテスト。"""
import datetime

from openpyxl import load_workbook

import config
from services import excel_export as xl
from services import slip_check as sc
from tests.helpers import FIELDS, GOOD_HEADER, NUMERIC, judged, normal_line

MASTER = sc.PartnerMaster([{"取引先コード": "100001", "取引先名": "株式会社サンプル商事", "別名": ""}])


def make_slip():
    discount = {"truth": {"金額": "-500"}, "extra": {"明細区分": "値引", "返品理由": "値引"}}
    lines = [normal_line(), normal_line(truth={"品番": "AB-2050", "枝番": "12", "数量": "1", "単価": "4500", "金額": "4500"}, extra={"良品不良": "不良", "返品理由": "初期不良"}), discount]
    return sc.build_slip(GOOD_HEADER, judged(lines), FIELDS, NUMERIC, MASTER, config.AMOUNT_TOLERANCE)


# ---- Excelの書式（取込フォーマット定義書） ----


def test_18_列の並びと見出しとシート名が定義書どおり(tmp_path):
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)
    recs = xl.slip_records(make_slip(), "返品伝票_1102.pdf", "2026/11/04 10:15:00")
    head, rows = xl.build_table(recs, fmt)
    path = str(tmp_path / "out.xlsx")
    xl.write_xlsx(path, fmt, head, rows)

    ws = load_workbook(path).active
    assert ws.title == "返品取込"
    assert [c.value for c in ws[1]] == [
        "伝票区分", "伝票番号", "返品日", "取引先コード", "取引先名", "行番号", "明細区分",
        "品番", "枝番", "数量", "単価", "金額", "返品理由", "良品不良区分", "元ファイル名", "確定日時",
    ]
    assert [c.value for c in ws[2]] == [
        "20", "R-000123", datetime.datetime(2026, 11, 2), "100001", "株式会社サンプル商事", 1, "1",
        "AB-1001", "01", 3, 1200, 3600, "サイズ違い", "1", "返品伝票_1102.pdf", "2026/11/04 10:15:00",
    ]
    # 不良品は区分 2
    assert ws.cell(row=3, column=14).value == "2"
    # 値引き行：明細区分 2、品番・数量・単価は空、金額はマイナス
    row4 = [c.value for c in ws[4]]
    assert row4[6] == "2"
    assert row4[7] in (None, "") and row4[9] is None and row4[10] is None
    assert row4[11] == -500


def test_枝番の先頭のゼロが数値に化けない(tmp_path):
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)
    head, rows = xl.build_table(xl.slip_records(make_slip(), "a.pdf", "2026/11/04 10:15:00"), fmt)
    path = str(tmp_path / "out.xlsx")
    xl.write_xlsx(path, fmt, head, rows)
    cell = load_workbook(path).active.cell(row=2, column=9)
    assert cell.value == "01"
    assert cell.number_format == "@"


# ---- ファイル名と出力履歴 ----


def test_出力ファイル名の形式():
    name = xl.export_file_name(datetime.datetime(2026, 11, 5, 9, 3, 7))
    assert name == "返品取込_20261105_090307.xlsx"


def test_出力履歴に追記でき重複チェック用に読める(tmp_path):
    path = str(tmp_path / "export_history.csv")
    xl.append_history(path, [{"出力日時": "2026/11/05 09:00:00", "取引先コード": "100001", "伝票番号": "R-1", "明細行数": 2}])
    xl.append_history(path, [{"出力日時": "2026/11/05 10:00:00", "取引先コード": "100001", "伝票番号": "R-2", "明細行数": 1}])
    assert xl.history_keys(path) == {("100001", "R-1"), ("100001", "R-2")}
    assert len(xl.load_history(path)) == 2


def test_出力履歴が無ければ空():
    assert xl.history_keys("存在しない.csv") == set()


# ---- セキュリティ ----


def test_イコールで始まる文字は数式にならず文字のまま出る(tmp_path):
    """伝票の文字に「=」があっても、Excelを開いたときに数式として実行されない（数式インジェクション対策）。"""
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)
    line = normal_line(extra={"返品理由": '=HYPERLINK("http://evil.example.com","クリック")'})
    slip = sc.build_slip(GOOD_HEADER, judged([line]), FIELDS, NUMERIC, MASTER, config.AMOUNT_TOLERANCE)
    head, rows = xl.build_table(xl.slip_records(slip, "a.pdf", "2026/11/04 10:15:00"), fmt)
    path = str(tmp_path / "out.xlsx")
    xl.write_xlsx(path, fmt, head, rows)
    cell = load_workbook(path).active.cell(row=2, column=13)
    assert cell.data_type == "s"
    assert cell.value.startswith("=HYPERLINK")
