"""チェックで共通に使う道具（値の決め方・数値と日付の読み方・会社名のそろえ方）のテスト。"""
from decimal import Decimal

import pytest

from services import slip_check as sc

# ---- F6 出す値の決め方 ----


def test_正解の入力がOCRより優先される():
    cell = {"truth": "NOTE-104", "ocr_text": "NOT E- 1 0 41", "adopt_ocr": True}
    assert sc.cell_value(cell, False) == "NOTE-104"


def test_正解が無くOCRを採用すればOCRの値を表記ゆれをそろえて使う():
    cell = {"truth": "", "ocr_text": "ＮＯＴＥ－１０４", "adopt_ocr": True}
    assert sc.cell_value(cell, False) == "NOTE-104"


def test_正解もOCR採用も無ければ未確定():
    assert sc.cell_value({"truth": "", "ocr_text": "NOTE-104"}, False) is None


def test_原本も空欄なら空文字():
    assert sc.cell_value({"truth": "", "truth_blank": True, "ocr_text": "x"}, False) == ""


# ---- 数値 ----


@pytest.mark.parametrize("text", ["1,200", "¥1,200", "１２００", "1200円", "1 2 0 0"])
def test_カンマや円記号や空白があっても数値として読める(text):
    assert sc.parse_number(text) == Decimal("1200")


@pytest.mark.parametrize("text", ["1O", "-", '" 1 0 0', ""])
def test_数値でなければNone(text):
    assert sc.parse_number(text) is None


# ---- 日付 ----


@pytest.mark.parametrize(
    "text",
    ["2026/11/2", "2026-11-02", "2026年11月2日", "令和8年11月2日", "R8.11.2", "２０２６／１１／０２", "2 0 2 6 / 1 1 / 0 2"],
)
def test_いろいろな日付の書き方がそろう(text):
    assert sc.parse_date(text) == "2026/11/02"


@pytest.mark.parametrize("text", ["2026/13/40", "2026/02/30", "きのう"])
def test_日付として正しくなければNone(text):
    assert sc.parse_date(text) is None


# ---- 会社名 ----


@pytest.mark.parametrize("name", ["(株)サンプル商事", "㈱サンプル商事", "（株）サンプル商事", "株式会社 サンプル商事"])
def test_株の書き方や空白が違っても同じ会社名になる(name):
    assert sc.partner_key(name) == sc.partner_key("株式会社サンプル商事")
