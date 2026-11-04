"""OCR結果と、人が確認した正解の比較・集計・CSV出力。

判定の種類:
  一致            … OCR結果と正解が文字どおり同じ
  一致（表記差）  … 表記ゆれ（全角半角・空白・ハイフンの種類・数値のカンマ等）を
                    そろえると同じ。どの揃え方を使ったかを記録する
  不一致          … 揃えても違う
  不一致（読めず）… 欄に対応する文字枠が無い／空（対象件数に含め、不正解として数える）
  未入力          … 正解がまだ入力されていない（集計の対象外）
"""
import csv
import io
import re
import unicodedata

MATCH = "一致"
MATCH_NOTATION = "一致（表記差）"
MISMATCH = "不一致"
UNREAD = "不一致（読めず）"
PENDING = "未入力"

# ー（長音）と 一（漢数字）は別の文字として扱うので含めない
_DASHES = "‐‑‒–—―−－"


def normalize(value, numeric):
    """比較用に表記をそろえる。戻り値: (そろえた文字列, 実際に効いた揃え方のリスト)"""
    rules = []
    s = value

    def step(new, name):
        nonlocal s
        if new != s:
            rules.append(name)
            s = new

    step(unicodedata.normalize("NFKC", s), "全角→半角")
    step(s.strip(), "前後の空白")
    step(re.sub(r"\s+", "", s), "途中の空白")
    step(re.sub(f"[{_DASHES}]", "-", s), "ハイフンの種類")
    if numeric:
        step(s.replace(",", ""), "桁区切りカンマ")
        step(re.sub(r"^[¥\\]|円$", "", s), "通貨記号")
    return s, rules


def judge(field, ocr_text, truth, truth_blank, numeric_fields):
    numeric = field in numeric_fields
    res = {"result": PENDING, "exact": False, "ocr_norm": "", "truth_norm": "", "rules": ""}
    if truth_blank:
        truth = ""
    elif truth.strip() == "":
        return res

    ocr_norm, ocr_rules = normalize(ocr_text, numeric)
    truth_norm, truth_rules = normalize(truth, numeric)
    res["ocr_norm"], res["truth_norm"] = ocr_norm, truth_norm

    if truth_blank:
        # 原本も空欄: OCRが何も読まなければ正解
        res["result"] = MATCH if ocr_norm == "" else MISMATCH
        res["exact"] = ocr_text == ""
        return res
    if ocr_norm == "":
        res["result"] = UNREAD
        return res
    if ocr_text == truth:
        res["result"], res["exact"] = MATCH, True
        return res
    if ocr_norm == truth_norm:
        res["result"] = MATCH_NOTATION
        parts = []
        if ocr_rules:
            parts.append("OCR側: " + "・".join(ocr_rules))
        if truth_rules:
            parts.append("正解側: " + "・".join(truth_rules))
        res["rules"] = " / ".join(parts)
        return res
    res["result"] = MISMATCH
    return res


def cell_ocr(items_by_no, nos):
    """割り当てた枠を左→右に並べて連結する。"""
    picked = sorted((items_by_no[n] for n in nos if n in items_by_no), key=lambda it: it["box"]["x"])
    return {
        "text": "".join(it["text"] for it in picked),
        "min_conf": min((it["confidence"] for it in picked), default=None),
        "nos": [it["no"] for it in picked],
    }


def judge_rows(rows, items, fields, numeric_fields):
    """保存形式の行に、OCR文字列と判定を付けた表示用の行を返す。"""
    items_by_no = {it["no"]: it for it in (items or [])}
    out = []
    for i, row in enumerate(rows, start=1):
        cells = {}
        for f in fields:
            c = row["cells"].get(f) or {}
            ocr = cell_ocr(items_by_no, c.get("ocr_nos", []))
            j = judge(f, ocr["text"], c.get("truth", ""), c.get("truth_blank", False), numeric_fields)
            cells[f] = {
                "ocr_nos": ocr["nos"],
                "assign": c.get("assign", "manual"),
                "truth": c.get("truth", ""),
                "truth_blank": bool(c.get("truth_blank", False)),
                "ocr_text": ocr["text"],
                "ocr_min_conf": ocr["min_conf"],
                **j,
            }
        out.append({"row": i, "no_text": row.get("no_text", ""), "cells": cells})
    return out


def _new_counter():
    return {"target": 0, "correct": 0, "exact": 0, "notation": 0, "mismatch": 0, "unread": 0, "pending": 0}


def _add(cnt, result):
    if result == PENDING:
        cnt["pending"] += 1
        return
    cnt["target"] += 1
    if result in (MATCH, MATCH_NOTATION):
        cnt["correct"] += 1
    if result == MATCH:
        cnt["exact"] += 1
    elif result == MATCH_NOTATION:
        cnt["notation"] += 1
    elif result == MISMATCH:
        cnt["mismatch"] += 1
    elif result == UNREAD:
        cnt["unread"] += 1


def _rates(cnt):
    t = cnt["target"]
    cnt["rate"] = round(cnt["correct"] / t, 4) if t else None
    cnt["exact_rate"] = round(cnt["exact"] / t, 4) if t else None
    return cnt


def summarize(judged_by_page, fields, page_count, ocr_done_pages):
    """judged_by_page: {page(int): judge_rows の結果}"""
    by_field = {f: _new_counter() for f in fields}
    total = _new_counter()
    by_page = []
    for p in range(1, page_count + 1):
        rows = judged_by_page.get(p)
        pf = {f: _new_counter() for f in fields}
        pt = _new_counter()
        for row in rows or []:
            for f in fields:
                r = row["cells"][f]["result"]
                for cnt in (by_field[f], total, pf[f], pt):
                    _add(cnt, r)
        by_page.append(
            {
                "page": p,
                "ocr_done": p in ocr_done_pages,
                "saved": rows is not None,
                "rows": len(rows or []),
                "fields": {f: _rates(c) for f, c in pf.items()},
                "total": _rates(pt),
            }
        )
    return {
        "fields": fields,
        "by_field": [{"field": f, **_rates(by_field[f])} for f in fields],
        "total": _rates(total),
        "by_page": by_page,
    }


def _pct(v):
    return "" if v is None else f"{v * 100:.1f}%"


def _xl(s):
    """Excelで数式として解釈されないよう、= + - @ で始まる文字列の先頭に ' を付ける。"""
    return "'" + s if s and s[0] in "=+-@\t\r" else s


def cells_csv(filename, judged_by_page, fields, saved_at_by_page):
    """全ページ・全欄の記録（1欄1行）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(
        [
            "ファイル", "ページ", "行", "No（OCR）", "項目",
            "OCR結果", "OCR信頼度（最小）", "使用した枠No", "割り当て",
            "正解（人が確認）", "原本も空欄", "比較用OCR", "比較用正解",
            "判定", "完全一致", "表記差の扱い", "保存日時",
        ]
    )
    for p in sorted(judged_by_page):
        for row in judged_by_page[p]:
            for f in fields:
                c = row["cells"][f]
                w.writerow(
                    [
                        filename, p, row["row"], _xl(row["no_text"]), f,
                        _xl(c["ocr_text"]),
                        "" if c["ocr_min_conf"] is None else f"{c['ocr_min_conf']:.4f}",
                        " ".join(str(n) for n in c["ocr_nos"]),
                        "自動" if c["assign"] == "auto" else "手動",
                        _xl(c["truth"]), "○" if c["truth_blank"] else "",
                        _xl(c["ocr_norm"]), _xl(c["truth_norm"]),
                        c["result"], "○" if c["exact"] else "", c["rules"],
                        saved_at_by_page.get(p, ""),
                    ]
                )
    return buf.getvalue()


def summary_csv(filename, summary):
    """項目別とページ別の集計。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    head = ["正しく読めた件数", "対象件数", "正解率", "うち完全一致", "うち表記差のみ", "不一致", "読めず", "完全一致率", "未入力（対象外）"]

    def vals(c):
        return [c["correct"], c["target"], _pct(c["rate"]), c["exact"], c["notation"], c["mismatch"], c["unread"], _pct(c["exact_rate"]), c["pending"]]

    w.writerow(["# 項目別", filename])
    w.writerow(["項目", *head])
    for c in summary["by_field"]:
        w.writerow([c["field"], *vals(c)])
    w.writerow(["全項目", *vals(summary["total"])])
    w.writerow([])
    w.writerow(["# ページ別"])
    w.writerow(["ページ", "OCR済", "記録済", "行数", "項目", *head])
    for p in summary["by_page"]:
        for f in summary["fields"]:
            w.writerow([p["page"], "○" if p["ocr_done"] else "", "○" if p["saved"] else "", p["rows"], f, *vals(p["fields"][f])])
        w.writerow([p["page"], "○" if p["ocr_done"] else "", "○" if p["saved"] else "", p["rows"], "ページ計", *vals(p["total"])])
    return buf.getvalue()
