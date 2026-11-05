"""テスト用の見本データを作る道具。OCRは実行せず、OCR結果（文字枠）を手で用意する。"""
import os

import config
from services import master_import as mi
from services import review as rv

FIELDS = config.REVIEW_FIELDS
NUMERIC = config.NUMERIC_FIELDS
SAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
NOW = "2026-11-05 10:00:00"


def load_samples(con):
    """samples/ の架空のマスタと売上データを、データベースに取り込む。"""
    for kind, name in [("partner", "取引先マスタ"), ("item", "商品マスタ"), ("reason", "返品理由マスタ"), ("sales", "売上データ")]:
        with open(os.path.join(SAMPLES, f"{name}.xlsx"), "rb") as f:
            r = mi.import_file(con, kind, f"{name}.xlsx", f.read(), NOW)
        assert r["imported"], (name, r["errors"])


def make_page(lines):
    """明細の見本から、保存形式の行と、OCRの文字枠（items）を作る。

    lines の各要素:
      {"truth": {"品番": "NOTE-104", ...},  人が入力した正解
       "ocr":   {"品番": "NOTE-104", ...},  OCRが読んだ文字
       "adopt": ["数量", ...],              OCR結果を採用した欄
       "blank": ["枝番", ...],              原本も空欄
       "extra": {"明細区分", "良品不良", "税率", "元売上"}}  人が選ぶ項目
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
    """問題のない通常の明細1行（売上 S-24001 の NOTE-104 を2冊返品）。必要なところだけ上書きして使う。"""
    truth = {"品番": "NOTE-104", "枝番": "01", "数量": "2", "単価": "240", "返品理由": "サイズ違い"}
    truth.update(over.pop("truth", {}))
    extra = {"明細区分": "通常", "良品不良": "良品"}
    extra.update(over.pop("extra", {}))
    return {"truth": truth, "extra": extra, **over}


def discount_line(price="-100", tax="10"):
    """値引の行。"""
    return {"truth": {"単価": price}, "blank": ["品番", "枝番", "数量", "返品理由"], "extra": {"明細区分": "値引", "税率": tax}}


# 元売上番号つきの伝票の見出し（架空）
GOOD_HEADER = {
    "会社名": "株式会社サンプル商事",
    "会社コード": "100001",
    "伝票番号": "R-000123",
    "返品日": "2026/11/02",
    "元売上番号": "S-24001",
}
