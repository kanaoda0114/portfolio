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


def _aligned_header(it, found, tol):
    """文字枠の左端か右端が、ちょうど1つの見出しとそろっていれば、その見出し名を返す。"""
    x0, x1 = it["box"]["x"], it["box"]["x"] + it["box"]["w"]
    hits = {
        lab
        for lab, h in found.items()
        if abs(h["box"]["x"] - x0) <= tol or abs(h["box"]["x"] + h["box"]["w"] - x1) <= tol
    }
    return hits.pop() if len(hits) == 1 else None


def empty_cell():
    return {"ocr_nos": [], "assign": "manual", "truth": "", "truth_blank": False}


def _infer_missing_headers(items, found, headers, missing):
    """読み間違えた見出しを、位置の並びから補う。

    見出しの行（確かに読めた見出しと同じ高さ）にある文字枠を左から並べ、確かに読めた見出しの間に
    「読めなかった見出しの数」とちょうど同じ数の枠があれば、その枠を順番どおりの見出しとみなす。
    （文字の中身からは推測しない。並び順と位置だけで決める）"""
    if len(found) < 2:
        return
    cy = median(_center_y(it) for it in found.values())
    h = median(it["box"]["h"] for it in found.values())
    row = sorted((it for it in items if abs(_center_y(it) - cy) <= h * 0.6), key=lambda it: it["box"]["x"])
    found_nos = {it["no"]: lab for lab, it in found.items()}
    order = [lab for lab in headers]  # 見出しの並び（左→右）
    # 確かに読めた見出しの位置（row の中の添字）と、見出しの並びの中の添字
    anchors = [(i, order.index(found_nos[it["no"]])) for i, it in enumerate(row) if it["no"] in found_nos and found_nos[it["no"]] in order]
    if anchors != sorted(anchors, key=lambda a: a[1]):
        return  # 並びが合わない（別の行を拾っている）
    bounds = [(-1, -1)] + anchors + [(len(row), len(order))]
    for (i0, o0), (i1, o1) in zip(bounds, bounds[1:]):
        gap_items = row[i0 + 1:i1]
        gap_labels = order[o0 + 1:o1]
        if gap_items and len(gap_items) == len(gap_labels):
            for it, lab in zip(gap_items, gap_labels):
                if lab in missing:
                    found[lab] = it


def propose_rows(items, headers, fields, end_labels, min_overlap, v_lines=None):
    """戻り値: {"status": "ok"|"no_header", "note": str, "rows": [...], "unassigned": [no, ...],
               "columns": {見出し: [左端, 右端]}, "bands": [[上端, 下端], ...]（行ごとの縦の範囲）}

    columns / bands は、欄を切り出して読み直す（ocr_refine）ときの位置の手がかり。"""
    # 見出しを探す（同じ見出しが複数あれば信頼度の高い方）
    found = {}
    for it in items:
        lab = _label(it["text"])
        if lab in headers and (lab not in found or it["confidence"] > found[lab]["confidence"]):
            found[lab] = it

    missing = [f for f in fields if f not in found]
    if missing:
        _infer_missing_headers(items, found, headers, missing)
        missing = [f for f in fields if f not in found]
    if missing:
        return {
            "status": "no_header",
            "note": f"明細の見出し（{'・'.join(missing)}）が読み取れなかったため、自動割り当てはしていません。"
            "「行を追加」して、各欄の枠を選んでください。",
            "rows": [],
            "unassigned": [],
            "columns": {},
            "bands": [],
        }

    # 列の範囲 = 罫線（v_lines）があればそれで区切る。無ければ隣り合う見出しの中心の中間点で区切る
    cols = sorted(found.items(), key=lambda kv: kv[1]["box"]["x"] + kv[1]["box"]["w"] / 2)
    centers = [it["box"]["x"] + it["box"]["w"] / 2 for _, it in cols]
    ranges = {}
    for i, (lab, it) in enumerate(cols):
        left = (centers[i - 1] + centers[i]) / 2 if i > 0 else float("-inf")
        right = (centers[i] + centers[i + 1]) / 2 if i < len(cols) - 1 else float("inf")
        if v_lines:
            # 見出しのすぐ左・すぐ右の罫線。ただし隣の見出しを越えた罫線は使わない
            l = max((x for x in v_lines if x < it["box"]["x"] and (i == 0 or x > centers[i - 1])), default=None)
            r = min((x for x in v_lines if x > it["box"]["x"] + it["box"]["w"] and (i == len(cols) - 1 or x < centers[i + 1])), default=None)
            left = l if l is not None else left
            right = r if r is not None else right
        ranges[lab] = (left, right)

    header_bottom = max(it["box"]["y"] + it["box"]["h"] for it in found.values())
    header_h = median(it["box"]["h"] for it in found.values())

    # 明細の終わり（「小計」の上端）。見つからなければページ下端まで
    # 「合計（税込）」のように後ろに文字が続いても、終わりの印として扱う
    ends = [
        it["box"]["y"]
        for it in items
        if any(_label(it["text"]).startswith(e) for e in end_labels) and it["box"]["y"] > header_bottom
    ]
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

    rows, unassigned, bands = [], [], []
    for g in groups:
        bands.append([min(it["box"]["y"] for it in g["items"]), max(it["box"]["y"] + it["box"]["h"] for it in g["items"])])
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
                # 列の境界をまたいでいても、見出しと左端（または右端）がそろっていれば、その列とみなす
                # （左寄せの品番や、右寄せの金額など、伝票によくある並べ方）
                aligned = _aligned_header(it, found, header_h)
                if aligned in fields:
                    cells[aligned]["ocr_nos"].append(it["no"])
                    cells[aligned]["assign"] = "auto"
                else:
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
    # 端の列の外側は画像の端まで（inf は JSON にできないので、十分大きな値にしておく）
    columns = {lab: [max(l, 0.0), min(r, 1e9)] for lab, (l, r) in ranges.items()}
    return {"status": "ok", "note": note, "rows": rows, "unassigned": unassigned, "columns": columns, "bands": bands}
