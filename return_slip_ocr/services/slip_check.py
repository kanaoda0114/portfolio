"""返品伝票のチェックで共通に使う道具（値の決め方・数値と日付の読み方・会社名のそろえ方）。

伝票1枚分の照合とチェックは services/returns.py（要件定義書 1.4版 F4・F9）。

出す値の決め方（F6）:
  正解が入力されている       → 正解の値（表記ゆれをそろえたもの）
  正解が未入力でOCRを採用した → OCRの値（表記ゆれをそろえたもの）
  どちらでもない             → 未確定（確定できない）
"""
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from .review import normalize

# 確定・出力を止める問題
ERROR = "エラー"
# 確定・出力はできるが、人に見てほしい問題
WARNING = "警告"


def cell_value(cell, numeric):
    """判定済みの欄（review.judge_rows の cells[f]）から、出す値を返す。未確定なら None。"""
    if cell.get("truth_blank"):
        return ""
    truth = cell.get("truth") or ""
    if truth.strip():
        return normalize(truth, numeric)[0]
    if cell.get("adopt_ocr") and (cell.get("ocr_text") or "").strip():
        return normalize(cell["ocr_text"], numeric)[0]
    return None


def parse_number(text):
    """カンマ・円記号・全角・空白をそろえて数値にする。数値でなければ None。"""
    s = normalize(text or "", True)[0]
    if s == "":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


_ERAS = {"令和": 2018, "平成": 1988}


def parse_date(text):
    """日付の文字を YYYY/MM/DD にそろえる。読めなければ None。

    対応: 2026/11/2・2026-11-02・2026.11.2・2026年11月2日・令和8年11月2日・R8.11.2
    """
    s = unicodedata.normalize("NFKC", text or "")
    s = re.sub(r"\s+", "", s)
    if not s:
        return None
    m = re.fullmatch(r"(令和|平成|R|H)(\d{1,2}|元)[年./-](\d{1,2})[月./-](\d{1,2})日?", s)
    if m:
        era = {"R": "令和", "H": "平成"}.get(m.group(1), m.group(1))
        year = _ERAS[era] + (1 if m.group(2) == "元" else int(m.group(2)))
        month, day = int(m.group(3)), int(m.group(4))
    else:
        m = re.fullmatch(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", s)
        if not m:
            return None
        year, month, day = (int(g) for g in m.groups())
    try:
        return date(year, month, day).strftime("%Y/%m/%d")
    except ValueError:
        return None


def partner_key(name):
    """取引先名の照合用キー: 全角半角・空白・(株)/株式会社 の違いをそろえる。"""
    s = unicodedata.normalize("NFKC", name or "")  # ㈱ → (株)、全角括弧 → 半角
    s = re.sub(r"\s+", "", s)
    for short, full in (("(株)", "株式会社"), ("(有)", "有限会社"), ("(合)", "合同会社")):
        s = s.replace(short, full)
    return s
