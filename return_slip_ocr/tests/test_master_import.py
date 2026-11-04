"""マスタ・売上データの取り込み（要件定義書 F8、データ設計書 4章）のテスト。"""
import io
import os

import pytest
from openpyxl import load_workbook

from services import db as dbm
from services import master_import as mi

NOW = "2026-11-05 10:00:00"
SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")

PARTNER_HEAD = "会社コード,会社名,会社名カナ,別名,インボイス登録番号,締日,支払サイト,消費税の端数処理,有効"
ITEM_HEAD = "品番,枝番,商品名,単位,税率,有効"
SALES_HEAD = "売上番号,会社コード,売上日,行番号,品番,枝番,数量,単価,金額"


@pytest.fixture
def con(tmp_path):
    c = dbm.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def csv_bytes(*lines, encoding="utf-8"):
    return ("\r\n".join(lines) + "\r\n").encode(encoding)


def imp(con, kind, *lines, name="data.csv", encoding="utf-8"):
    return mi.import_file(con, kind, name, csv_bytes(*lines, encoding=encoding), NOW)


def load_basic(con):
    assert imp(con, "partner", PARTNER_HEAD, "100001,株式会社サンプル商事,,サンプル商事,T1234567890123,20,翌月末,切り捨て,")["imported"]
    assert imp(con, "item", ITEM_HEAD, "NOTE-104,01,A5ノート,冊,10,", "TEA-001,,緑茶,箱,8,")["imported"]


def count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---- 正常な取り込み ----


def test_取引先マスタを取り込める(con):
    load_basic(con)
    row = con.execute("SELECT * FROM m_partner").fetchone()
    assert (row["partner_code"], row["name"], row["closing_day"], row["tax_rounding"], row["active"]) == (
        "100001", "株式会社サンプル商事", 20, "切り捨て", 1
    )


def test_締日に末と書くと99になる(con):
    r = imp(con, "partner", PARTNER_HEAD, "100002,テスト物産株式会社,,,,末,翌々月10日,四捨五入,1")
    assert r["imported"]
    assert con.execute("SELECT closing_day FROM m_partner").fetchone()[0] == 99


def test_売上データを取り込むと売上と売上明細に分かれる(con):
    load_basic(con)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,10,240,", "S-1,100001,2026/10/01,2,TEA-001,,5,540,2700")
    assert r["imported"], r["errors"]
    assert (count(con, "t_sales"), count(con, "t_sales_line")) == (1, 2)
    line = con.execute("SELECT amount FROM t_sales_line WHERE line_no = 1").fetchone()
    assert line[0] == 2400  # 金額が空なら 数量×単価


def test_サンプルのExcelをすべて取り込める(con):
    for kind, name in [("partner", "取引先マスタ"), ("item", "商品マスタ"), ("reason", "返品理由マスタ"), ("sales", "売上データ")]:
        with open(os.path.join(SAMPLES, f"{name}.xlsx"), "rb") as f:
            r = mi.import_file(con, kind, f"{name}.xlsx", f.read(), NOW)
        assert r["imported"], (name, r["errors"])
    # Excelで文字列として入れた枝番「01」の先頭のゼロが消えない
    assert con.execute("SELECT branch_no FROM m_item WHERE item_no = 'NOTE-104'").fetchone()[0] == "01"


def test_ShiftJISのCSVも取り込める(con):
    r = imp(con, "partner", PARTNER_HEAD, "100001,株式会社サンプル商事,,,,20,翌月末,切り捨て,1", encoding="cp932")
    assert r["imported"]
    assert con.execute("SELECT name FROM m_partner").fetchone()[0] == "株式会社サンプル商事"


def test_使わない列は無視してお知らせする(con):
    r = imp(con, "item", ITEM_HEAD + ",備考", "NOTE-104,01,A5ノート,冊,10,,メモ")
    assert r["imported"]
    assert "備考" in r["notices"][0]


# ---- エラーなら1行も入れない ----


def test_1行でもエラーがあれば何も取り込まない(con):
    r = imp(con, "partner", PARTNER_HEAD, "100001,A社,,,,20,翌月末,切り捨て,", "100002,B社,,,,40,翌月末,切り捨て,")
    assert not r["imported"]
    assert count(con, "m_partner") == 0
    assert r["errors"] == [{"row": 3, "column": "締日", "message": "1〜28、または末日（99）にしてください"}]


@pytest.mark.parametrize(
    "line, column",
    [
        ("12345,A社,,,,20,翌月末,切り捨て,", "会社コード"),  # 6桁でない
        ("100001,,,,,20,翌月末,切り捨て,", "会社名"),  # 必須が空
        ("100001,A社,,,X123,20,翌月末,切り捨て,", "インボイス登録番号"),
        ("100001,A社,,,,20,翌月末,五捨六入,", "消費税の端数処理"),
        ("100001,A社,,,,20,翌月末,切り捨て,たぶん", "有効"),
    ],
)
def test_取引先マスタの値のチェック(con, line, column):
    r = imp(con, "partner", PARTNER_HEAD, line)
    assert not r["imported"]
    assert r["errors"][0]["column"] == column
    assert r["errors"][0]["row"] == 2


def test_必須の列が無いファイルは取り込まない(con):
    r = imp(con, "item", "品番,商品名", "NOTE-104,A5ノート")
    assert not r["imported"]
    assert "税率" in r["errors"][0]["column"]


def test_ファイルの中で同じキーが2回出てきたらエラー(con):
    r = imp(con, "item", ITEM_HEAD, "NOTE-104,01,A5ノート,冊,10,", "NOTE-104,01,A5ノート青,冊,10,")
    assert not r["imported"]
    assert "2行目と同じ" in r["errors"][0]["message"]


def test_イコールで始まる値は取り込まない(con):
    r = imp(con, "item", ITEM_HEAD, '=HYPERLINK("x"),01,A5ノート,冊,10,')
    assert not r["imported"]


# ---- 売上データの参照チェック ----


def test_取引先マスタに無い会社の売上は取り込まない(con):
    load_basic(con)
    r = imp(con, "sales", SALES_HEAD, "S-1,999999,2026/10/01,1,NOTE-104,01,10,240,")
    assert not r["imported"]
    assert r["errors"][0]["column"] == "会社コード"


def test_商品マスタに無い品番の売上は取り込まない(con):
    load_basic(con)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-999,01,10,240,")
    assert not r["imported"]
    assert r["errors"][0]["column"] == "品番・枝番"


def test_同じ売上番号で会社や売上日が違う行はエラー(con):
    load_basic(con)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,1,240,", "S-1,100001,2026/10/02,2,TEA-001,,1,540,")
    assert not r["imported"]


def test_金額が数量かける単価と合わなければエラー(con):
    load_basic(con)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,10,240,2500")
    assert not r["imported"]
    assert r["errors"][0]["column"] == "金額"


# ---- 上書きと入れ替え ----


def test_マスタはキーが同じなら上書きする(con):
    load_basic(con)
    assert imp(con, "item", ITEM_HEAD, "NOTE-104,01,A5ノート 新,冊,10,0")["imported"]
    row = con.execute("SELECT name, active FROM m_item WHERE item_no = 'NOTE-104'").fetchone()
    assert (row[0], row[1]) == ("A5ノート 新", 0)
    assert count(con, "m_item") == 2


def test_売上は売上番号ごとに入れ替える(con):
    load_basic(con)
    imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,10,240,", "S-1,100001,2026/10/01,2,TEA-001,,5,540,")
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,8,240,")
    assert r["imported"]
    assert r["saved"]["replaced_sales"] == 1
    assert count(con, "t_sales_line") == 1
    assert con.execute("SELECT qty FROM t_sales_line").fetchone()[0] == 8


def _add_return(con, qty):
    """返品済みの記録を1件つくる（返品の確定はC周で作るため、ここでは直接入れる）。"""
    con.execute("INSERT INTO m_return_reason (reason_code, name) VALUES ('01', 'サイズ違い')")
    con.execute(
        """INSERT INTO t_return_slip (partner_code, slip_no, return_date, fiscal_month, closing_date, source_file, status, confirmed_at)
           VALUES ('100001', 'R-1', '2026-11-02', '2026-11', '2026-11-20', 'a.pdf', '確定済み', ?)""",
        (NOW,),
    )
    con.execute(
        """INSERT INTO t_return_line (return_id, line_no, line_kind, sales_no, sales_line_no, item_no, branch_no, qty, unit_price, amount, tax_mode, tax_rate, reason_code, condition)
           VALUES (1, 1, '通常', 'S-1', 1, 'NOTE-104', '01', ?, 240, ?, '10', 10, '01', '良品')""",
        (qty, qty * 240),
    )
    con.commit()


def test_返品済みの数より少なくなる入れ替えはしない(con):
    load_basic(con)
    imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,10,240,")
    _add_return(con, 6)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,5,240,")
    assert not r["imported"]
    assert "すでに 6 個返品" in r["errors"][0]["message"]
    assert con.execute("SELECT qty FROM t_sales_line").fetchone()[0] == 10


def test_返品済みの明細をなくす入れ替えはしない(con):
    load_basic(con)
    imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,10,240,", "S-1,100001,2026/10/01,2,TEA-001,,5,540,")
    _add_return(con, 2)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,2,TEA-001,,5,540,")
    assert not r["imported"]


def test_返品済みでも数を減らさない入れ替えはできる(con):
    load_basic(con)
    imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,10,240,")
    _add_return(con, 6)
    r = imp(con, "sales", SALES_HEAD, "S-1,100001,2026/10/01,1,NOTE-104,01,12,240,")
    assert r["imported"], r["errors"]
    assert count(con, "t_return_line") == 1


# ---- 見本Excel ----


@pytest.mark.parametrize("kind", list(mi.DATASETS))
def test_見本Excelの1行目が列名どおり(kind):
    ws = load_workbook(io.BytesIO(mi.template_xlsx(kind))).worksheets[0]
    assert [c.value for c in ws[1]] == [c.name for c in mi.DATASETS[kind].cols]


@pytest.mark.parametrize("name", ["data.txt", "data.pdf"])
def test_ExcelとCSV以外は取り込まない(con, name):
    with pytest.raises(Exception) as e:
        mi.import_file(con, "item", name, b"x", NOW)
    assert getattr(e.value, "code", "") == "import_bad_type"
