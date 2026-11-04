"""伝票1枚分の「出す値」を決め、取込前の入力チェック（要件定義書 F4・F6）を行う。

このモジュールは画面やファイルに依存しない（テストしやすくするため）。

出す値の決め方（F6）:
  正解が入力されている       → 正解の値（表記ゆれをそろえたもの）
  正解が未入力でOCRを採用した → OCRの値（表記ゆれをそろえたもの）
  どちらでもない             → 未確定（確定できない）
"""
import csv
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from .review import normalize

# 確定・出力を止める問題
ERROR = "エラー"
# 確定・出力はできるが、人に見てほしい問題
WARNING = "警告"

HEADER_FIELDS = ("伝票番号", "返品日", "取引先名")


# ---------------------------------------------------------------------------
# 値の変換
# ---------------------------------------------------------------------------


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
    """カンマ・円記号・全角をそろえて数値にする。数値でなければ None。"""
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


# ---------------------------------------------------------------------------
# 取引先マスタ
# ---------------------------------------------------------------------------


def partner_key(name):
    """取引先名の照合用キー: 全角半角・空白・(株)/株式会社 の違いをそろえる。"""
    s = unicodedata.normalize("NFKC", name or "")  # ㈱ → (株)、全角括弧 → 半角
    s = re.sub(r"\s+", "", s)
    for short, full in (("(株)", "株式会社"), ("(有)", "有限会社"), ("(合)", "合同会社")):
        s = s.replace(short, full)
    return s


class PartnerMaster:
    """取引先名 → 取引先コード。近い名前を推測で選ぶことはしない。"""

    def __init__(self, rows):
        self._by_key = {}
        for r in rows:
            code = (r.get("取引先コード") or "").strip()
            names = [r.get("取引先名") or ""] + (r.get("別名") or "").split(";")
            for n in names:
                if n.strip():
                    self._by_key[partner_key(n)] = code

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8-sig", newline="") as f:
            return cls(list(csv.DictReader(f)))

    def code_of(self, name):
        return self._by_key.get(partner_key(name))


# ---------------------------------------------------------------------------
# 伝票のチェック
# ---------------------------------------------------------------------------


def _issue(level, check, message, page=None, row=None):
    return {"level": level, "check": check, "message": message, "page": page, "row": row}


def _is_empty_row(row, fields):
    """何も入力・割り当てされていない行（集計からも出力からも除く）。"""
    cells_empty = all(
        not row["cells"][f].get("truth", "").strip()
        and not row["cells"][f].get("truth_blank")
        and not (row["cells"][f].get("ocr_text") or "").strip()
        for f in fields
    )
    ex = row.get("extra") or {}
    return cells_empty and not ex.get("返品理由") and not ex.get("良品不良")


def build_slip(header, judged_by_page, fields, numeric_fields, master, tolerance, history_keys=()):
    """伝票1枚分の出力行と、チェック結果を作る。

    header         : {"伝票番号", "返品日", "取引先名"}（人が入力した値）
    judged_by_page : {ページ番号: review.judge_rows() の結果}
    history_keys   : 出力済みの (取引先コード, 伝票番号) の集まり（C6 用）
    戻り値: {"header": {...}, "lines": [...], "issues": [...], "errors": n, "warnings": n}
    """
    issues = []
    header = header or {}

    # ---- 伝票ヘッダ ----
    slip_no = normalize(header.get("伝票番号") or "", False)[0]
    if not slip_no:
        issues.append(_issue(ERROR, "C1", "伝票番号が入力されていません。"))

    raw_date = header.get("返品日") or ""
    return_date = parse_date(raw_date)
    if not raw_date.strip():
        issues.append(_issue(ERROR, "C1", "返品日が入力されていません。"))
    elif return_date is None:
        issues.append(_issue(ERROR, "C4", f"返品日「{raw_date}」を日付として読めません。"))

    partner_name = (header.get("取引先名") or "").strip()
    partner_code = None
    if not partner_name:
        issues.append(_issue(ERROR, "C1", "取引先名が入力されていません。"))
    else:
        partner_code = master.code_of(partner_name)
        if partner_code is None:
            issues.append(
                _issue(ERROR, "C5", f"取引先「{partner_name}」が取引先マスタにありません（似た名前は選びません）。")
            )

    if slip_no and partner_code and (partner_code, slip_no) in set(history_keys):
        issues.append(_issue(WARNING, "C6", f"伝票番号 {slip_no} は、この取引先ですでに出力されています。"))

    # ---- 明細 ----
    lines = []
    for page in sorted(judged_by_page):
        for row in judged_by_page[page]:
            if _is_empty_row(row, fields):
                continue
            r = row["row"]
            ex = row.get("extra") or {}
            kind = ex.get("明細区分") or "通常"
            discount = kind == "値引"

            values = {f: cell_value(row["cells"][f], f in numeric_fields) for f in fields}
            undecided = [f for f, v in values.items() if v is None]
            # 値引き行は品番・枝番・数量・単価を使わない
            if discount:
                undecided = [f for f in undecided if f == "金額"]
            if undecided:
                issues.append(
                    _issue(
                        ERROR,
                        "C1",
                        f"未確定の欄があります（{'・'.join(undecided)}）。正解を入力するか、OCR結果を採用してください。",
                        page,
                        r,
                    )
                )

            required = ["金額"] if discount else ["品番", "数量", "単価", "金額"]
            for f in required:
                if values.get(f) == "":
                    issues.append(_issue(ERROR, "C1", f"{f}が空欄です。", page, r))
            if not ex.get("返品理由", "").strip():
                issues.append(_issue(ERROR, "C1", "返品理由が入力されていません。", page, r))
            if not discount and not ex.get("良品不良"):
                issues.append(_issue(ERROR, "C1", "良品／不良が選ばれていません。", page, r))

            nums = {}
            for f in ("数量", "単価", "金額"):
                if discount and f != "金額":
                    continue
                v = values.get(f)
                if not v:
                    continue
                n = parse_number(v)
                if n is None:
                    issues.append(_issue(ERROR, "C2", f"{f}「{v}」が数値ではありません。", page, r))
                elif f in ("数量", "金額") and n != n.to_integral_value():
                    issues.append(_issue(ERROR, "C2", f"{f}「{v}」が整数ではありません。", page, r))
                else:
                    nums[f] = n

            if not discount and len(nums) == 3:
                diff = abs(nums["数量"] * nums["単価"] - nums["金額"])
                if diff > Decimal(str(tolerance)):
                    issues.append(
                        _issue(
                            WARNING,
                            "C3",
                            f"数量×単価（{nums['数量'] * nums['単価']}）と金額（{nums['金額']}）が {diff} 円違います。",
                            page,
                            r,
                        )
                    )

            lines.append(
                {
                    "page": page,
                    "row": r,
                    "明細区分": kind,
                    "品番": "" if discount else (values.get("品番") or ""),
                    "枝番": "" if discount else (values.get("枝番") or ""),
                    "数量": None if discount else nums.get("数量"),
                    "単価": None if discount else nums.get("単価"),
                    "金額": nums.get("金額"),
                    "返品理由": ex.get("返品理由", "").strip(),
                    "良品不良": "" if discount else ex.get("良品不良", ""),
                }
            )

    if not lines:
        issues.append(_issue(ERROR, "C1", "明細が1行もありません。"))

    return {
        "header": {
            "伝票番号": slip_no,
            "返品日": return_date,
            "取引先コード": partner_code,
            "取引先名": partner_name,
        },
        "lines": lines,
        "issues": issues,
        "errors": sum(i["level"] == ERROR for i in issues),
        "warnings": sum(i["level"] == WARNING for i in issues),
    }
