"""マスタの値で直す（services/candidates.py）と、見出しの1文字違い（header_extract）のテスト。"""
from services import candidates as cd
from services import header_extract as hx

ITEMS = ["NOTE-104", "PEN-2203", "FILE-0551", "FILE-0552", "TEA-001"]
REASONS = ["サイズ違い", "色違い", "初期不良", "数量違い", "注文取消", "その他"]


def test_match_code_swaps_confusable_letters_only():
    assert cd.match_code("TEA-OO1", ITEMS) == "TEA-001"
    assert cd.match_code("NOTE-1O4", ITEMS) == "NOTE-104"
    assert cd.match_code("tea-001", ITEMS) == "TEA-001"  # 大文字小文字・全角半角はそろえる
    assert cd.match_code("FILE-0553", ITEMS) is None  # 1文字違いでも「別の品番」なので直さない
    assert cd.match_code("", ITEMS) is None


def test_match_code_gives_up_when_ambiguous():
    # 「FILE-055I」は I→1 で FILE-0551 になるが、S→5 の入れ替えなどで候補が2つ以上にはならないので一意
    assert cd.match_code("FILE-055I", ITEMS) == "FILE-0551"
    assert cd.match_code("X-S", ["X-5", "X-$"]) is None  # 2つに当たる → 決めない


def test_match_text_fixes_one_char_difference_only():
    assert cd.match_text("サイス違い", REASONS) == "サイズ違い"
    assert cd.match_text("数鼻違い", REASONS) == "数量違い"
    assert cd.match_text("三の他", REASONS) == "その他"
    assert cd.match_text("飲岳佐い", REASONS) is None  # 3文字違い → 人が選ぶ
    assert cd.match_text("違い", REASONS) is None  # 短すぎる


def test_apply_known_marks_fixed_boxes():
    items = [
        {"no": 1, "text": "TEA-OO1", "confidence": 0.5, "box": {"x": 0, "y": 0, "w": 1, "h": 1}},
        {"no": 2, "text": "サイス違い", "confidence": 0.5, "box": {"x": 0, "y": 0, "w": 1, "h": 1}},
        {"no": 3, "text": "5-24001", "confidence": 0.5, "box": {"x": 0, "y": 0, "w": 1, "h": 1}},
    ]
    rows = [{"cells": {"品番": {"ocr_nos": [1]}, "返品理由": {"ocr_nos": [2]}, "枝番": {"ocr_nos": []}, "数量": {"ocr_nos": []}, "単価": {"ocr_nos": []}}}]
    header = {"元売上番号": {"value": "5-24001", "ocr_nos": [3], "confidence": 0.5}}
    cd.apply_known(items, rows, header, {"品番": ITEMS, "返品理由": REASONS, "元売上番号": ["S-24001", "S-24015"]})
    assert items[0]["text"] == "TEA-001" and items[0]["matched"] is True
    assert items[1]["text"] == "サイズ違い" and items[1]["matched"] is True
    assert items[2]["text"] == "S-24001" and items[2]["matched"] is True


def test_apply_known_skips_cells_made_of_several_boxes():
    items = [{"no": 1, "text": "TEA-", "confidence": 0.5, "box": {}}, {"no": 2, "text": "OO1", "confidence": 0.5, "box": {}}]
    rows = [{"cells": {"品番": {"ocr_nos": [1, 2]}, "返品理由": {"ocr_nos": []}}}]
    cd.apply_known(items, rows, {}, {"品番": ITEMS})
    assert items[0]["text"] == "TEA-" and "matched" not in items[0]


def _it(no, text, x, y, w=100, h=20, conf=0.9):
    return {"no": no, "text": text, "confidence": conf, "points": [], "box": {"x": x, "y": y, "w": w, "h": h}}


def test_header_label_tolerates_one_misread_char():
    items = [
        _it(1, "伝票希号", 400, 20, conf=0.02), _it(2, "R-446110", 520, 20),
        _it(3, "取引先コート 100001", 50, 60, w=250),
        _it(4, "返品日", 400, 60, w=60), _it(5, "2026/11/28", 480, 60),
    ]
    got = hx.proposal_values(hx.extract_header(items))
    assert got["伝票番号"] == "R-446110"
    assert got["会社コード"] == "100001"
    assert got["返品日"] == "2026/11/28"


def test_header_short_label_is_not_fuzzy():
    # 「返品日」（3文字）は1文字違いを許さない（「返品月」などを拾わないため）
    items = [_it(1, "返品月", 400, 60, w=60), _it(2, "2026/11", 480, 60)]
    assert hx.proposal_values(hx.extract_header(items))["返品日"] == ""
