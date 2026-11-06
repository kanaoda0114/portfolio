"""明細の欄を1つずつ切り出して読み直し、OCRの精度を上げる（Issue #19）。

最初のOCR（ページ全体）は、文字の場所を見つけるのは得意だが、
  - 「-100」の「-」を「"」と読む、「NOTE-104」を「NOT E- 1 0 41」と読む（列の罫線を 1 と読む）
  - 小さい「1」を見落とす
といった間違いがある。そこで、列の種類が分かっている欄は
  1. 欄の画像だけを切り出し、小さければ拡大して
  2. その列に出てくる文字だけ（数量なら数字だけ、品番なら英数字とハイフン）に絞って
読み直す。効果は tools/ocr_benchmark.py で測る（docs/test_reports/OCR精度_*.md）。

やらないこと
  - 文字の中身から推測して値を埋めること（読めない欄は空のまま、人が入力する）
  - 返品理由など、日本語の欄の読み直し（絞れる文字が無い）
"""
import re

import cv2
import numpy as np

import config
from . import header_extract as hx
from .ocr_engine import recognize_crop


def _crop(image, x0, y0, x1, y1):
    h, w = image.shape[:2]
    x0, y0 = max(int(x0), 0), max(int(y0), 0)
    x1, y1 = min(int(x1), w), min(int(y1), h)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return image[y0:y1, x0:x1]


def _prepare(crop):
    """グレースケールにし、文字が小さければ拡大する（EasyOCR は高さ 64px 前後が読みやすい）。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    scale = config.REFINE_TARGET_HEIGHT / max(gray.shape[0], 1)
    if scale > 1.05:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray


def has_ink(crop, min_ratio):
    """切り出した欄に文字らしい黒い部分があるか（空欄に罫線のかけらを「3」などと読まないため）。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    return (binary > 0).mean() >= min_ratio


def value_starts(crop, value_len):
    """見出しと値が1つの枠に入っているとき、値が始まる x（枠の左端からのピクセル）の候補を、確からしい順に返す。

    文字の無い縦の帯（空白）のうち、行の高さの 1/4 以上の幅があるものを区切りの候補にし、
    右側の幅が「値の文字数ぶんの幅（英数字1文字 ≒ 高さの 0.45 倍）」に近い順に並べる。候補が無ければ空のリスト。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    h, w = gray.shape[:2]
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    ink = (binary > 0).sum(axis=0) > 0
    min_gap = max(int(h * 0.25), 3)
    expected = value_len * h * 0.45
    cands = []
    x = 0
    while x < w:
        if not ink[x]:
            start = x
            while x < w and not ink[x]:
                x += 1
            right_w = w - x
            if x - start >= min_gap and start > 0 and 0.5 * expected <= right_w <= 2.0 * expected:
                cands.append((abs(right_w - expected), x))
        else:
            x += 1
    return [x for _, x in sorted(cands)]


def value_start(crop, value_len):
    """value_starts の最有力候補（無ければ None）。"""
    c = value_starts(crop, value_len)
    return c[0] if c else None


def _tight(crop):
    """文字のある範囲だけに詰める（右寄せの数字の左に広い余白があると、EasyOCR が余計な文字を読むため）。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    ys, xs = np.where(binary > 0)
    if len(xs) == 0:
        return crop
    h = crop.shape[0]
    m = max(int(h * 0.3), 3)
    x0, x1 = max(xs.min() - m, 0), min(xs.max() + m + 1, crop.shape[1])
    return crop[:, x0:x1]


def _read(image, reader, box, allowlist):
    """欄の範囲 box=(x0,y0,x1,y1) を読み直す。戻り値: (文字, 信頼度) 読めなければ ("", None)"""
    crop = _crop(image, *box)
    if crop is None:
        return "", None
    text, conf = recognize_crop(_prepare(_tight(crop)), reader, allowlist)
    text = re.sub(r"\s+", "", text)
    return text, conf


def _union(items_by_no, nos):
    boxes = [items_by_no[n]["box"] for n in nos if n in items_by_no]
    if not boxes:
        return None
    x0 = min(b["x"] for b in boxes)
    y0 = min(b["y"] for b in boxes)
    x1 = max(b["x"] + b["w"] for b in boxes)
    y1 = max(b["y"] + b["h"] for b in boxes)
    return x0, y0, x1, y1


def _pad(box, h_ratio, w_ratio):
    x0, y0, x1, y1 = box
    h = y1 - y0
    return x0 - h * w_ratio, y0 - h * h_ratio, x1 + h * w_ratio, y1 + h * h_ratio


def _new_item(items, text, conf, box):
    x0, y0, x1, y1 = box
    it = {
        "no": max((it["no"] for it in items), default=0) + 1,
        "text": text,
        "confidence": round(float(conf), 4),
        "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "box": {"x": x0, "y": y0, "w": round(x1 - x0, 1), "h": round(y1 - y0, 1)},
        "refined": True,
    }
    items.append(it)
    return it


def refine_cells(image, items, proposal, reader):
    """割り当て済みの欄を読み直して items の text / confidence を置き換える。
    何も割り当たらなかった欄は、列×行の範囲を読んで、読めたら新しい枠として割り当てる。"""
    by_no = {it["no"]: it for it in items}
    columns = proposal.get("columns") or {}
    bands = proposal.get("bands") or []
    for row, band in zip(proposal["rows"], bands):
        for field, allow in config.REFINE_ALLOWLIST.items():
            cell = row["cells"].get(field)
            if cell is None or field not in columns:
                continue
            if cell["ocr_nos"]:
                box = _pad(_union(by_no, cell["ocr_nos"]), 0.3, 0.15)
                text, conf = _read(image, reader, box, allow)
                if not text:
                    continue
                # 複数の枠に分かれていた欄は、1つ目の枠にまとめ、残りは空にする（枠の番号は変えない）
                first = by_no[cell["ocr_nos"][0]]
                first["text"], first["confidence"], first["refined"] = text, round(float(conf), 4), True
                for n in cell["ocr_nos"][1:]:
                    by_no[n]["text"], by_no[n]["refined"] = "", True
            else:
                left, right = columns[field]
                y0, y1 = band
                h = y1 - y0
                m = 6.0  # 罫線を欄に入れない（傾いていても罫線の位置は行の高さの範囲ではほとんどずれない）
                box = (left + m, y0 - h * 0.2, right - m, y1 + h * 0.2)
                crop = _crop(image, *box)
                if crop is None or not has_ink(crop, config.REFINE_MIN_INK):
                    continue  # 空欄
                text, conf = _read(image, reader, box, allow)
                if text and conf is not None and conf >= config.REFINE_MIN_CONF_NEW:
                    it = _new_item(items, text, conf, box)
                    by_no[it["no"]] = it
                    cell["ocr_nos"] = [it["no"]]
                    cell["assign"] = "auto"
    return items


def refine_header(image, items, reader):
    """見出し（会社コード・伝票番号・返品日・元売上番号）の値の枠を、文字を絞って読み直す。
    見出しの文字と値が1つの枠に入っているときは触らない。"""
    found = hx.extract_header(items)
    by_no = {it["no"]: it for it in items}
    for field, allow in config.REFINE_HEADER_ALLOWLIST.items():
        f = found.get(field)
        if not f or len(f["ocr_nos"]) != 1:
            continue
        it = by_no[f["ocr_nos"][0]]
        b = it["box"]
        whole = hx._norm(it["text"])
        if whole == f["value"]:
            box = (b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"])
            text, conf = _read(image, reader, _pad(box, 0.3, 0.15), allow)
            if text:
                it["text"], it["confidence"], it["refined"] = text, round(float(conf), 4), True
        elif whole.endswith(f["value"]) and f["value"]:
            # 「元伝票番号 $-76369」のように見出しと値が1つの枠: 見出しと値の間の空白で切り、右側だけ読み直す
            crop = _crop(image, b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"])
            text, conf = "", None
            # 区切りの候補を確からしい順に試し、最初のOCRと同じ文字数で読めたものを採用する（最大3つ）
            for split in (value_starts(crop, len(f["value"])) if crop is not None else [])[:3]:
                box = (b["x"] + split, b["y"], b["x"] + b["w"], b["y"] + b["h"])
                t, c = _read(image, reader, _pad(box, 0.3, 0.1), allow)
                if t and len(t) == len(f["value"]):
                    text, conf = t, c
                    break
                if t and not text:
                    text, conf = t, c
            if text:
                it["text"] = it["text"][: len(it["text"]) - len(f["value"])] + text if it["text"].endswith(f["value"]) else f"{whole[: -len(f['value'])]} {text}"
                it["confidence"], it["refined"] = round(float(conf), 4), True
    return items
