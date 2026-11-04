"""OCR結果の文字枠を、明細表の「行 × 項目」に割り当てる候補を作る。

割り当ては位置だけで行い、文字の内容から値を推測・補完することはしない。
- 見出し（品番・枝番…）が見つからない → 自動割り当てしない（人が枠を選ぶ）
- 枠が列の境界をまたいでいる → その枠は「未割り当て」に残す
"""
import re
import unicodedata
from statistics import median


def _label(text):
    """見出し比較用: 全角→半角、空白と末尾の記号を除く（「No.」→「No」）。"""
    s = unicodedata.normalize("NFKC", text)
    s = re.sub(r"\s+", "", s)
    return s.rstrip(".:：")


def _center_y(it):
    return it["box"]["y"] + it["box"]["h"] / 2


def empty_cell():
    return {"ocr_nos": [], "assign": "manual", "truth": "", "truth_blank": False}


def propose_rows(items, headers, fields, end_labels, min_overlap):
    """戻り値: {"status": "ok"|"no_header", "note": str, "rows": [...], "unassigned": [no, ...]}"""
    # 見出しを探す（同じ見出しが複数あれば信頼度の高い方）
    found = {}
    for it in items:
        lab = _label(it["text"])
        if lab in headers and (lab not in found or it["confidence"] > found[lab]["confidence"]):
            found[lab] = it

    missing = [f for f in fields if f not in found]
    if missing:
        return {
            "status": "no_header",
            "note": f"明細の見出し（{'・'.join(missing)}）が読み取れなかったため、自動割り当てはしていません。"
            "「行を追加」して、各欄の枠を選んでください。",
            "rows": [],
            "unassigned": [],
        }

    # 列の範囲 = 隣り合う見出しの中心の中間点で区切る
    cols = sorted(found.items(), key=lambda kv: kv[1]["box"]["x"] + kv[1]["box"]["w"] / 2)
    centers = [it["box"]["x"] + it["box"]["w"] / 2 for _, it in cols]
    ranges = {}
    for i, (lab, _) in enumerate(cols):
        left = (centers[i - 1] + centers[i]) / 2 if i > 0 else float("-inf")
        right = (centers[i] + centers[i + 1]) / 2 if i < len(cols) - 1 else float("inf")
        ranges[lab] = (left, right)

    header_bottom = max(it["box"]["y"] + it["box"]["h"] for it in found.values())
    header_h = median(it["box"]["h"] for it in found.values())

    # 明細の終わり（「小計」の上端）。見つからなければページ下端まで
    ends = [it["box"]["y"] for it in items if _label(it["text"]) in end_labels and it["box"]["y"] > header_bottom]
    table_end = min(ends) if ends else float("inf")

    header_nos = {it["no"] for it in found.values()}
    body = [
        it
        for it in items
        if it["no"] not in header_nos and it["box"]["y"] >= header_bottom - 2 and _center_y(it) < table_end
    ]

    # 縦位置の近い枠を同じ行にまとめる
    body.sort(key=_center_y)
    groups = []
    for it in body:
        if groups and _center_y(it) - groups[-1]["cy"] <= header_h * 1.2:
            g = groups[-1]
            g["items"].append(it)
            g["cy"] = sum(_center_y(x) for x in g["items"]) / len(g["items"])
        else:
            groups.append({"cy": _center_y(it), "items": [it]})

    rows, unassigned = [], []
    for g in groups:
        cells = {f: empty_cell() for f in fields}
        no_texts = []
        for it in sorted(g["items"], key=lambda x: x["box"]["x"]):
            x0, w = it["box"]["x"], max(it["box"]["w"], 1)
            best, best_ratio = None, 0.0
            for lab, (left, right) in ranges.items():
                ratio = max(0.0, min(x0 + w, right) - max(x0, left)) / w
                if ratio > best_ratio:
                    best, best_ratio = lab, ratio
            if best_ratio < min_overlap:
                unassigned.append(it["no"])
            elif best in fields:
                cells[best]["ocr_nos"].append(it["no"])
                cells[best]["assign"] = "auto"
            elif best == "No":
                no_texts.append(it["text"])
            # 品名など対象外の列は割り当てない
        rows.append({"no_text": "".join(no_texts), "cells": cells})

    note = f"見出しの位置から {len(rows)} 行を自動で割り当てました（「自動」の欄）。原本と見比べて、違う場合は枠を選び直してください。"
    if not ends:
        note += " 「小計」が見つからなかったため、見出しより下の文字をすべて明細として扱っています。"
    if unassigned:
        note += f" 列の境界をまたぐ枠 {len(unassigned)} 個は割り当てていません。"
    return {"status": "ok", "note": note, "rows": rows, "unassigned": unassigned}
