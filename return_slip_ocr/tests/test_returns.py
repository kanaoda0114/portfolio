"""マスタ・売上との照合とチェック（要件定義書 1.4版 F4・F9）のテスト。

架空のサンプルデータ（samples/）を使う。主な売上:
  S-24001（2026/10/01・100001）NOTE-104/01 10冊 240円、PEN-2203/12 30本 120円
  S-24102（2026/10/28・100001）NOTE-104/01 5冊 250円
  S-24020（2026/10/05・100002）PEN-2203/12 50本 115円
"""
from datetime import date

import pytest

from services import returns as rt
from services.slip_check import ERROR, WARNING
from tests.helpers import GOOD_HEADER, discount_line, judged, normal_line

TODAY = date(2026, 11, 5)


def check(con, lines, header=None, today=TODAY, exclude=None):
    return rt.build_return(con, GOOD_HEADER if header is None else header, judged(lines), today, exclude)


def codes(result, level=None):
    return [i["check"] for i in result["issues"] if level is None or i["level"] == level]


def add_return(con, sales_no, line_no, qty, slip_no="R-OLD", item=("NOTE-104", "01")):
    """すでに確定済みの返品を1件入れる。"""
    cur = con.execute(
        """INSERT INTO t_return_slip (partner_code, slip_no, return_date, fiscal_month, closing_date, source_file, status, confirmed_at)
           VALUES ('100001', ?, '2026-10-30', '2026-11', '2026-11-20', 'old.pdf', '確定済み', '2026-10-30 10:00:00')""",
        (slip_no,),
    )
    con.execute(
        """INSERT INTO t_return_line (return_id, line_no, line_kind, sales_no, sales_line_no, item_no, branch_no, qty, unit_price, amount, tax_mode, tax_rate, reason_code, condition)
           VALUES (?, 1, '通常', ?, ?, ?, ?, ?, 240, ?, '10', 10, '01', '良品')""",
        (cur.lastrowid, sales_no, line_no, item[0], item[1], qty, qty * 240),
    )
    con.commit()
    return cur.lastrowid


# ---- 正常な伝票 ----


def test_問題のない伝票はエラーも警告もなくマスタの値がつく(sample_db):
    r = check(sample_db, [normal_line()])
    assert r["issues"] == []
    h = r["header"]
    assert (h["会社コード"], h["会社名"], h["インボイス登録番号"]) == ("100001", "株式会社サンプル商事", "T1234567890123")
    ln = r["lines"][0]
    assert (ln["商品名"], ln["税率"], ln["数量"], ln["金額"], ln["理由コード"]) == ("A5ノート 横罫", 10, 2, 480, "01")
    assert ln["元売上"]["sales_no"] == "S-24001"


# ---- 会社の特定（S1・S2） ----


def test_会社コードが無くても会社名の別名から会社を特定できる(sample_db):
    r = check(sample_db, [normal_line()], header={**GOOD_HEADER, "会社コード": "", "会社名": "(株)サンプル商事"})
    assert r["header"]["会社コード"] == "100001"
    assert codes(r) == []


def test_マスタに無い会社はエラーで似た名前を選ばない(sample_db):
    r = check(sample_db, [normal_line()], header={**GOOD_HEADER, "会社コード": "", "会社名": "サンプル商事株式会社"})
    assert "S1" in codes(r, ERROR)


def test_会社コードと会社名が別の会社を指していたらエラー(sample_db):
    r = check(sample_db, [normal_line()], header={**GOOD_HEADER, "会社コード": "100002"})
    assert "S2" in codes(r, ERROR)


# ---- 元の売上の特定（S3） ----


def test_元売上番号が無く候補が2件なら人が選ぶまでエラー(sample_db):
    r = check(sample_db, [normal_line()], header={**GOOD_HEADER, "元売上番号": ""})
    assert "S3" in codes(r, ERROR)
    assert [c["sales_no"] for c in r["lines"][0]["候補"]] == ["S-24102", "S-24001"]  # 新しい順


def test_候補から人が選べば照合できる(sample_db):
    line = normal_line(extra={"元売上": {"sales_no": "S-24001", "line_no": 1}})
    r = check(sample_db, [line], header={**GOOD_HEADER, "元売上番号": ""})
    assert codes(r) == []
    assert r["lines"][0]["元売上"]["sales_no"] == "S-24001"


def test_候補が1件だけなら照合先として表示する(sample_db):
    line = normal_line(truth={"品番": "PEN-2203", "枝番": "12", "単価": "120"})
    r = check(sample_db, [line], header={**GOOD_HEADER, "元売上番号": ""})
    assert codes(r) == []
    assert r["lines"][0]["元売上"]["sales_no"] == "S-24001"  # 別の会社の S-24020 は候補にならない


def test_元売上番号が別の会社の売上ならエラー(sample_db):
    line = normal_line(truth={"品番": "PEN-2203", "枝番": "12", "単価": "120"})
    r = check(sample_db, [line], header={**GOOD_HEADER, "元売上番号": "S-24020"})
    assert any("別の会社" in i["message"] for i in r["issues"])


def test_返品日より後の売上は候補にならない(sample_db):
    r = check(sample_db, [normal_line()], header={**GOOD_HEADER, "返品日": "2026/09/30", "元売上番号": ""})
    assert "S3" in codes(r, ERROR)
    assert r["lines"][0]["候補"] == []


# ---- 商品・数量・単価（S4〜S6） ----


def test_商品マスタに無い品番はエラー(sample_db):
    r = check(sample_db, [normal_line(truth={"品番": "NOTE-1041"})])  # OCRの読み間違いの例
    assert "S4" in codes(r, ERROR)


def test_売った数より多く返品するとエラー(sample_db):
    r = check(sample_db, [normal_line(truth={"数量": "11"})])
    assert "S5" in codes(r, ERROR)


def test_すでに返品した分は返品できる残りから引く(sample_db):
    add_return(sample_db, "S-24001", 1, 8)
    r = check(sample_db, [normal_line(truth={"数量": "3"})])
    assert r["lines"][0]["元売上"]["remaining"] == 2
    assert "S5" in codes(r, ERROR)


def test_全部返品済みの売上は元売上番号が無いとき候補から外す(sample_db):
    add_return(sample_db, "S-24001", 1, 10)
    r = check(sample_db, [normal_line()], header={**GOOD_HEADER, "元売上番号": ""})
    assert [c["sales_no"] for c in r["lines"][0]["候補"]] == ["S-24102"]


def test_同じ伝票の中で同じ売上を2行返品すると合計で数える(sample_db):
    r = check(sample_db, [normal_line(truth={"数量": "6"}), normal_line(truth={"数量": "6"})])
    assert codes(r, ERROR) == ["S5"]


def test_単価が売ったときと違えば警告で確定はできる(sample_db):
    r = check(sample_db, [normal_line(truth={"単価": "230"})])
    assert codes(r) == ["S6"]
    assert r["errors"] == 0


def test_自分自身の確定済みの返品は残りの計算から除く(sample_db):
    rid = add_return(sample_db, "S-24001", 1, 10, slip_no="R-000123")
    r = check(sample_db, [normal_line()], exclude=rid)
    assert "S5" not in codes(r)
    assert "C6" not in codes(r)


# ---- 返品理由（C3） ----


def test_返品理由の別の書き方もコードにできる(sample_db):
    r = check(sample_db, [normal_line(truth={"返品理由": "サイズ間違い"})])
    assert r["lines"][0]["理由コード"] == "01"
    assert r["lines"][0]["返品理由"] == "サイズ違い"


def test_マスタに無い返品理由はエラー(sample_db):
    r = check(sample_db, [normal_line(truth={"返品理由": "なんとなく"})])
    assert "C3" in codes(r, ERROR)


# ---- 必須・数値（C1・C2） ----


def test_良品不良が未選択ならエラー(sample_db):
    r = check(sample_db, [normal_line(extra={"良品不良": ""})])
    assert "C1" in codes(r, ERROR)


def test_OCRを採用していない欄があれば未確定でエラー(sample_db):
    line = normal_line(truth={"品番": ""}, ocr={"品番": "NOT E- 1 0 41"})
    r = check(sample_db, [line])
    assert any("未確定" in i["message"] for i in r["issues"])


@pytest.mark.parametrize("qty", ["0", "1.5", "-1"])
def test_数量は1以上の整数(sample_db, qty):
    r = check(sample_db, [normal_line(truth={"数量": qty})])
    assert "C2" in codes(r, ERROR)


# ---- 値引の行（Q11・Q15） ----


def test_値引の行は売上と照合せず税率を選べば問題なし(sample_db):
    r = check(sample_db, [normal_line(), discount_line()])
    assert codes(r) == []
    d = r["lines"][1]
    assert (d["明細区分"], d["数量"], d["金額"], d["税率"], d["元売上"]) == ("値引", 1, -100, 10, None)


def test_値引の税率が未選択ならエラー(sample_db):
    r = check(sample_db, [normal_line(), discount_line(tax="")])
    assert "C1" in codes(r, ERROR)


def test_値引がプラスの金額ならエラー(sample_db):
    r = check(sample_db, [normal_line(), discount_line(price="100")])  # マイナスが読めなかった例
    assert "C2" in codes(r, ERROR)


# ---- 計上月と締め日（Q10・Q14） ----


def test_締め日までに確定すれば返品日の月に計上(sample_db):
    h = check(sample_db, [normal_line()])["header"]
    assert (h["fiscal_month"], h["closing_date"], h["late"]) == ("2026-11", "2026-11-20", False)


def test_締め日を過ぎて確定すると次の月に計上して警告(sample_db):
    r = check(sample_db, [normal_line()], today=date(2026, 11, 22))
    assert (r["header"]["fiscal_month"], r["header"]["late"]) == ("2026-12", True)
    assert codes(r, WARNING) == ["S8"]


@pytest.mark.parametrize(
    "day, closing_day, expected",
    [
        (date(2026, 11, 20), 20, date(2026, 11, 20)),
        (date(2026, 11, 21), 20, date(2026, 12, 20)),
        (date(2026, 12, 25), 20, date(2027, 1, 20)),  # 年をまたぐ
        (date(2026, 2, 10), 99, date(2026, 2, 28)),  # 末日締め
    ],
)
def test_締め日の計算(day, closing_day, expected):
    assert rt.closing_date_for(day, closing_day) == expected


# ---- 重複（C6） ----


def test_同じ会社と伝票番号がすでに確定済みなら警告(sample_db):
    add_return(sample_db, "S-24102", 1, 1, slip_no="R-000123")
    r = check(sample_db, [normal_line()])
    assert "C6" in codes(r, WARNING)


# ---- 税率ごとの合計と消費税（Q9・Q15） ----


def _ln(kind, amount, rate=None, mode=None):
    return {"明細区分": kind, "金額": amount, "税率": rate, "税率の決め方": mode or (str(rate) if rate else "")}


def test_単価は税込なので消費税は10割る110で取り出す():
    s = rt.tax_summary([_ln("通常", 480, 10), _ln("通常", 1200, 10), _ln("値引", -100, mode="10")], "切り捨て")
    r10 = s["rates"][0]
    assert (r10["rate"], r10["total"], r10["tax"], r10["base"]) == (10, 1580, 143, 1437)  # 1580×10÷110=143.63…
    assert s["total"] == 1580  # 消費税は合計の中に入っている（足さない）


@pytest.mark.parametrize("rounding, tax", [("切り捨て", 143), ("四捨五入", 144), ("切り上げ", 144)])
def test_消費税の端数処理は取引先ごと(rounding, tax):
    s = rt.tax_summary([_ln("通常", 1580, 10)], rounding)
    assert s["rates"][0]["tax"] == tax


def test_按分は税率ごとの金額の比率で値引を分ける():
    s = rt.tax_summary([_ln("通常", 1000, 10), _ln("通常", 500, 8), _ln("値引", -150, mode="按分")], "切り捨て")
    by = {r["rate"]: r for r in s["rates"]}
    assert (by[10]["discounts"][0]["amount"], by[8]["discounts"][0]["amount"]) == (-100, -50)
    assert (by[10]["total"], by[8]["total"]) == (900, 450)


def test_按分の端数は金額が大きい税率に寄せて合計は元の値引額と同じ():
    s = rt.tax_summary([_ln("通常", 1000, 10), _ln("通常", 500, 8), _ln("値引", -100, mode="按分")], "切り捨て")
    by = {r["rate"]: r for r in s["rates"]}
    a10, a8 = by[10]["discounts"][0]["amount"], by[8]["discounts"][0]["amount"]
    assert (a10, a8) == (-67, -33)  # −33.33… は 8% に −33、残り −67 を 10% へ
    assert a10 + a8 == -100


def test_商品が10パーセントだけなら按分しても全部10パーセント():
    s = rt.tax_summary([_ln("通常", 480, 10), _ln("通常", 1200, 10), _ln("値引", -100, mode="按分")], "切り捨て")
    assert [(r["rate"], r["total"]) for r in s["rates"]] == [(10, 1580)]
