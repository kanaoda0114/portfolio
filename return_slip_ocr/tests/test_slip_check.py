"""入力チェック（要件定義書 F4: C1〜C6）と、出す値の決め方（F6）のテスト。

テスト番号はテスト計画（Issue #4）の No に対応する。
"""
from decimal import Decimal

import pytest

import config
from services import slip_check as sc
from tests.helpers import FIELDS, GOOD_HEADER, NUMERIC, judged, normal_line

MASTER = sc.PartnerMaster(
    [
        {"取引先コード": "100001", "取引先名": "株式会社サンプル商事", "別名": "サンプル商事"},
        {"取引先コード": "100002", "取引先名": "テスト物産株式会社", "別名": ""},
    ]
)


def check(lines, header=None, history=()):
    return sc.build_slip(
        GOOD_HEADER if header is None else header,
        judged(lines),
        FIELDS,
        NUMERIC,
        MASTER,
        config.AMOUNT_TOLERANCE,
        history,
    )


def codes(result, level=None):
    return [i["check"] for i in result["issues"] if level is None or i["level"] == level]


# ---- 正常な伝票 ----


def test_問題のない伝票はエラーも警告もない():
    r = check([normal_line()])
    assert r["issues"] == []
    assert r["header"] == {
        "伝票番号": "R-000123",
        "返品日": "2026/11/02",
        "取引先コード": "100001",
        "取引先名": "株式会社サンプル商事",
    }
    line = r["lines"][0]
    assert (line["品番"], line["数量"], line["単価"], line["金額"]) == ("AB-1001", 3, 1200, 3600)


# ---- C1 必須項目 ----


def test_01_品番が空欄ならエラー():
    r = check([normal_line(truth={"品番": ""}, blank=["品番"])])
    assert "C1" in codes(r, sc.ERROR)


def test_02_値引き行は品番が空欄でもエラーにならない():
    discount = {"truth": {"金額": "-500"}, "extra": {"明細区分": "値引", "返品理由": "値引"}}
    r = check([normal_line(), discount])
    assert codes(r, sc.ERROR) == []
    assert r["lines"][1]["金額"] == -500


def test_03_良品不良が未選択ならエラー():
    r = check([normal_line(extra={"良品不良": ""})])
    assert any("良品" in i["message"] for i in r["issues"] if i["level"] == sc.ERROR)


def test_返品理由が空欄ならエラー():
    r = check([normal_line(extra={"返品理由": ""})])
    assert any("返品理由" in i["message"] for i in r["issues"])


@pytest.mark.parametrize("field", ["伝票番号", "返品日", "取引先名"])
def test_ヘッダ項目が空欄ならエラー(field):
    r = check([normal_line()], header={**GOOD_HEADER, field: ""})
    assert "C1" in codes(r, sc.ERROR)


def test_明細が1行もなければエラー():
    r = check([])
    assert "C1" in codes(r, sc.ERROR)


# ---- C2 数値 ----


def test_04_数量にアルファベットのOが混じるとエラー():
    r = check([normal_line(truth={"数量": "1O"})])
    assert "C2" in codes(r, sc.ERROR)


@pytest.mark.parametrize("text", ["1,200", "¥1,200", "１２００", "1200円"])
def test_05_カンマや円記号があっても数値として通る(text):
    r = check([normal_line(truth={"数量": "1", "単価": text, "金額": "1200"})])
    assert codes(r) == []
    assert r["lines"][0]["単価"] == Decimal("1200")


# ---- C3 金額の計算 ----


def test_06_数量かける単価と金額が一致すれば問題なし():
    assert codes(check([normal_line()])) == []


def test_07_1円以内のずれは許容する():
    r = check([normal_line(truth={"数量": "3", "単価": "33.3", "金額": "100"})])  # 99.9
    assert codes(r) == []


def test_08_2円以上ずれると警告で確定はできる():
    r = check([normal_line(truth={"金額": "3602"})])
    assert codes(r, sc.WARNING) == ["C3"]
    assert r["errors"] == 0


def test_09_値引き行は計算チェックをしない():
    discount = {"truth": {"数量": "1", "単価": "100", "金額": "-500"}, "extra": {"明細区分": "値引", "返品理由": "値引"}}
    r = check([normal_line(), discount])
    assert "C3" not in codes(r)


# ---- C4 日付 ----


@pytest.mark.parametrize(
    "text",
    ["2026/11/2", "2026-11-02", "2026年11月2日", "令和8年11月2日", "R8.11.2", "２０２６／１１／０２"],
)
def test_10_いろいろな日付の書き方がそろう(text):
    r = check([normal_line()], header={**GOOD_HEADER, "返品日": text})
    assert r["header"]["返品日"] == "2026/11/02"
    assert codes(r) == []


@pytest.mark.parametrize("text", ["2026/13/40", "2026/02/30", "きのう"])
def test_11_日付として正しくなければエラー(text):
    r = check([normal_line()], header={**GOOD_HEADER, "返品日": text})
    assert "C4" in codes(r, sc.ERROR)


# ---- C5 取引先 ----


@pytest.mark.parametrize("name", ["(株)サンプル商事", "㈱サンプル商事", "（株）サンプル商事", "サンプル商事", "株式会社 サンプル商事"])
def test_12_別名や株の書き方が違ってもコードに変換できる(name):
    r = check([normal_line()], header={**GOOD_HEADER, "取引先名": name})
    assert r["header"]["取引先コード"] == "100001"


def test_13_マスタに無い名前はエラーで近い名前を推測しない():
    r = check([normal_line()], header={**GOOD_HEADER, "取引先名": "株式会社サンプル商"})
    assert "C5" in codes(r, sc.ERROR)
    assert r["header"]["取引先コード"] is None


# ---- C6 重複 ----


def test_14_出力済みの伝票番号は警告():
    r = check([normal_line()], history={("100001", "R-000123")})
    assert codes(r, sc.WARNING) == ["C6"]


def test_取引先が違えば同じ伝票番号でも警告しない():
    r = check([normal_line()], history={("100002", "R-000123")})
    assert codes(r) == []


# ---- F6 出す値の決め方 ----


def test_15_正解の入力がOCRより優先される():
    line = normal_line(ocr={"品番": "AB-1OO1"})
    r = check([line])
    assert r["lines"][0]["品番"] == "AB-1001"


def test_15_正解が無くOCRを採用すればOCRの値を使う():
    line = normal_line(truth={"品番": ""}, ocr={"品番": "ＡＢ－１００１"}, adopt=["品番"])
    r = check([line])
    assert codes(r) == []
    assert r["lines"][0]["品番"] == "AB-1001"  # 表記ゆれはそろえる


def test_15_正解もOCR採用も無ければ未確定でエラー():
    line = normal_line(truth={"品番": ""}, ocr={"品番": "AB-1001"})
    r = check([line])
    assert any("未確定" in i["message"] for i in r["issues"])


def test_何も入力していない行は無視する():
    empty = {"truth": {}, "extra": {"返品理由": "", "良品不良": ""}}
    r = check([normal_line(), empty])
    assert len(r["lines"]) == 1
    assert codes(r) == []
