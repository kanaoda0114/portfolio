"""テスト用の見本データを作る道具。OCRは実行せず、OCR結果（文字枠）を手で用意する。"""
import config
from services import review as rv

FIELDS = config.REVIEW_FIELDS
NUMERIC = config.NUMERIC_FIELDS


def make_page(lines):
    """明細の見本から、保存形式の行と、OCRの文字枠（items）を作る。

    lines の各要素:
      {"truth": {"品番": "AB-1", ...},   人が入力した正解
       "ocr":   {"品番": "AB-1", ...},   OCRが読んだ文字
       "adopt": ["数量", ...],           OCR結果を採用した欄
       "extra": {"返品理由": ..., "良品不良": ..., "明細区分": ...}}
    """
    items, rows, no = [], [], 0
    for ln in lines:
        cells = {}
        for f in FIELDS:
            nos = []
            text = (ln.get("ocr") or {}).get(f)
            if text is not None:
                no += 1
                items.append({"no": no, "text": text, "confidence": 0.9, "box": {"x": no, "y": 0, "w": 1, "h": 1}})
                nos = [no]
            cells[f] = {
                "ocr_nos": nos,
                "assign": "manual",
                "truth": (ln.get("truth") or {}).get(f, ""),
                "truth_blank": f in (ln.get("blank") or []),
                "adopt_ocr": f in (ln.get("adopt") or []),
            }
        rows.append({"no_text": "", "cells": cells, "extra": rv.empty_extra(ln.get("extra"))})
    return rows, items


def judged(lines):
    rows, items = make_page(lines)
    return {1: rv.judge_rows(rows, items, FIELDS, NUMERIC)}


def normal_line(**over):
    """問題のない通常の明細1行（必要なところだけ上書きして使う）。"""
    truth = {"品番": "AB-1001", "枝番": "01", "数量": "3", "単価": "1200", "金額": "3600"}
    truth.update(over.pop("truth", {}))
    extra = {"明細区分": "通常", "返品理由": "サイズ違い", "良品不良": "良品"}
    extra.update(over.pop("extra", {}))
    return {"truth": truth, "extra": extra, **over}


GOOD_HEADER = {"伝票番号": "R-000123", "返品日": "2026/11/02", "取引先名": "株式会社サンプル商事"}
