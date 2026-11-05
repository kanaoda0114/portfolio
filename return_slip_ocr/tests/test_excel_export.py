"""取込用Excelの作成と出力履歴（要件定義書 F5）のテスト。"""
import datetime

from openpyxl import load_workbook

import config
from services import excel_export as xl
from services import returns as rt
from tests.helpers import GOOD_HEADER, discount_line, judged, normal_line

TODAY = datetime.date(2026, 11, 5)


def make_slip(con, lines=None):
    if lines is None:
        pen = normal_line(truth={"品番": "PEN-2203", "枝番": "12", "数量": "1", "単価": "120", "返品理由": "初期不良"}, extra={"良品不良": "不良"})
        lines = [normal_line(), pen, discount_line()]
    return rt.build_return(con, GOOD_HEADER, judged(lines), TODAY)


# ---- Excelの書式（取込フォーマット定義書） ----


def test_18_列の並びと見出しとシート名が定義書どおり(tmp_path, sample_db):
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)
    recs = xl.slip_records(make_slip(sample_db), "返品伝票_1102.pdf", "2026/11/04 10:15:00")
    head, rows = xl.build_table(recs, fmt)
    path = str(tmp_path / "out.xlsx")
    xl.write_xlsx(path, fmt, head, rows)

    ws = load_workbook(path).active
    assert ws.title == "返品取込"
    assert [c.value for c in ws[1]] == [
        "伝票区分", "計上月", "伝票番号", "返品日", "取引先コード", "取引先名", "行番号", "明細区分", "品番", "枝番",
        "数量", "単価（税込）", "金額（税込）", "税率", "理由コード", "返品理由", "良品不良区分", "元売上番号", "売上日",
        "元ファイル名", "確定日時",
    ]
    assert [c.value for c in ws[2]] == [
        "20", "2026/11", "R-000123", datetime.datetime(2026, 11, 2), "100001", "株式会社サンプル商事", 1, "1", "NOTE-104", "01",
        2, 240, 480, 10, "01", "サイズ違い", "1", "S-24001", datetime.datetime(2026, 10, 1),
        "返品伝票_1102.pdf", "2026/11/04 10:15:00",
    ]
    # 不良品は区分 2、理由コード 03
    assert (ws.cell(row=3, column=17).value, ws.cell(row=3, column=15).value) == ("2", "03")
    # 値引き行：明細区分 2、品番は空、数量1・単価と金額はマイナス、元売上は無し（1.4版 Q11）
    row4 = [c.value for c in ws[4]]
    assert row4[7] == "2"
    assert row4[8] in (None, "")
    assert (row4[10], row4[11], row4[12], row4[13]) == (1, -100, -100, 10)
    assert row4[17] in (None, "") and row4[18] is None


def test_枝番の先頭のゼロが数値に化けない(tmp_path, sample_db):
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)
    head, rows = xl.build_table(xl.slip_records(make_slip(sample_db), "a.pdf", "2026/11/04 10:15:00"), fmt)
    path = str(tmp_path / "out.xlsx")
    xl.write_xlsx(path, fmt, head, rows)
    cell = load_workbook(path).active.cell(row=2, column=10)  # 枝番
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


def test_イコールで始まる文字は数式にならず文字のまま出る(tmp_path, sample_db):
    """伝票の文字に「=」があっても、Excelを開いたときに数式として実行されない（数式インジェクション対策）。"""
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)
    line = normal_line(truth={"返品理由": '=HYPERLINK("http://evil.example.com","クリック")'})
    slip = make_slip(sample_db, [line])
    head, rows = xl.build_table(xl.slip_records(slip, "a.pdf", "2026/11/04 10:15:00"), fmt)
    path = str(tmp_path / "out.xlsx")
    xl.write_xlsx(path, fmt, head, rows)
    cell = load_workbook(path).active.cell(row=2, column=16)  # 返品理由
    assert cell.data_type == "s"
    assert cell.value.startswith("=HYPERLINK")
