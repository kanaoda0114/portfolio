"""罫線の検出（services/table_lines.py）と、罫線・見出しの並びを使った列の割り当て（table_mapper）のテスト。"""
import cv2
import numpy as np

import config
from services.table_lines import column_bounds, vertical_lines
from services.table_mapper import propose_rows


def _slip_image(angle=0.0):
    """白い紙に、明細表の縦線4本と短い文字っぽい縦棒を描いた画像。"""
    img = np.full((1000, 800, 3), 255, dtype=np.uint8)
    for x in (100, 300, 500, 700):
        cv2.line(img, (x, 200), (x, 500), (90, 90, 90), 2)
    cv2.line(img, (400, 250), (400, 262), (0, 0, 0), 2)  # 文字の縦棒（短い）は線ではない
    if angle:
        m = cv2.getRotationMatrix2D((400, 500), angle, 1.0)
        img = cv2.warpAffine(img, m, (800, 1000), borderValue=(255, 255, 255))
    return img


def test_vertical_lines_finds_table_borders_not_text():
    xs = vertical_lines(_slip_image())
    assert len(xs) == 4
    assert all(abs(x - want) <= 3 for x, want in zip(xs, (100, 300, 500, 700)))


def test_vertical_lines_tolerates_slight_tilt():
    xs = vertical_lines(_slip_image(angle=0.8))
    assert len(xs) == 4


def test_vertical_lines_empty_when_no_lines():
    assert vertical_lines(np.full((500, 500, 3), 255, dtype=np.uint8)) == []


def test_column_bounds():
    assert column_bounds(250, [100, 300, 500]) == (100, 300)
    assert column_bounds(50, [100, 300]) == (None, 100)


def _it(no, text, x, y, w=40, h=20, conf=0.9):
    return {"no": no, "text": text, "confidence": conf, "points": [], "box": {"x": x, "y": y, "w": w, "h": h}}


HEADERS = [_it(1, "No", 20, 50, w=20), _it(2, "品番", 60, 50), _it(3, "枝番", 200, 50), _it(4, "品名", 260, 50),
           _it(5, "数量", 420, 50), _it(6, "単価", 480, 50), _it(7, "金額", 560, 50), _it(8, "返品理由", 640, 50, w=80)]


def _propose(items, v_lines=None):
    return propose_rows(items, config.TABLE_HEADERS, config.REVIEW_FIELDS, config.TABLE_END_LABELS, config.AUTO_ASSIGN_MIN_OVERLAP, v_lines)


def test_right_aligned_number_needs_ruled_lines():
    """右寄せの「30」（466〜486）は、見出しの中間（数量 440 と 単価 520 の中間 = 480）をまたぐので
    罫線なしでは割り当てられない。罫線（数量の列は 410〜490）があれば数量に入る。"""
    headers = [it if it["text"] != "単価" else _it(6, "単価", 500, 50) for it in HEADERS]
    qty = _it(9, "30", 466, 100, w=20)
    p = _propose(headers + [qty])
    assert p["rows"][0]["cells"]["数量"]["ocr_nos"] == [] and 9 in p["unassigned"]
    p = _propose(headers + [qty], v_lines=[10, 50, 190, 250, 410, 490, 550, 630, 760])
    assert p["rows"][0]["cells"]["数量"]["ocr_nos"] == [9]
    assert p["columns"]["数量"] == [410, 490]


def test_misread_header_is_inferred_from_position():
    """「数量」を「ブ#」と読み間違えても、見出しの並び（品名と単価の間に1つ）から数量とみなす。"""
    items = [it if it["text"] != "数量" else _it(5, "ブ#", 420, 50, conf=0.01) for it in HEADERS]
    items.append(_it(9, "2", 440, 100, w=10))
    p = _propose(items)
    assert p["status"] == "ok"
    assert p["rows"][0]["cells"]["数量"]["ocr_nos"] == [9]


def test_header_not_inferred_when_count_differs():
    """見出しの行に余計な枠があって数が合わないときは、推測しない（人が枠を選ぶ）。"""
    items = [it for it in HEADERS if it["text"] != "数量"]
    items += [_it(5, "ブ#", 420, 50, conf=0.01), _it(10, "計", 450, 50, conf=0.5)]
    p = _propose(items)
    assert p["status"] == "no_header"
