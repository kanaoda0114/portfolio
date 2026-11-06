"""欄の読み直し（services/ocr_refine.py）のテスト。EasyOCR は使わず、読み直しの結果を手で決めた偽の reader で確かめる。"""
import numpy as np

import config
from services import ocr_refine as orf
from services.table_mapper import propose_rows

IMAGE = np.full((400, 800, 3), 255, dtype=np.uint8)
# 数量の列（枝番と単価の見出しの中間 = x 290〜410）の明細1行目（y 100〜120）に、文字のような黒い部分を描く
IMAGE[103:118, 335:352] = 0
# 見出しと値が同じ枠（x 50〜250, y 60〜80）: 見出し 55〜150、空白、値 170〜245
IMAGE[63:78, 55:150] = 0
IMAGE[63:78, 170:245] = 0


class FakeReader:
    """recognize() が呼ばれるたびに、answers から順に (文字, 信頼度) を返す。allowlist も記録する。"""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def recognize(self, gray, horizontal_list=None, free_list=None, allowlist=None, detail=1):
        self.calls.append({"shape": gray.shape, "allowlist": allowlist})
        if not self.answers:
            return []
        text, conf = self.answers.pop(0)
        return [([[0, 0], [1, 0], [1, 1], [0, 1]], text, conf)]


def _item(no, text, x, y, w=60, h=20, conf=0.9):
    return {"no": no, "text": text, "confidence": conf, "points": [], "box": {"x": x, "y": y, "w": w, "h": h}}


def _page(with_qty=True):
    """見出し1行＋明細1行の文字枠。数量は with_qty=False なら最初のOCRで見つからなかったことにする。"""
    items = [
        _item(1, "品番", 50, 50), _item(2, "枝番", 200, 50), _item(3, "数量", 320, 50),
        _item(4, "単価", 440, 50), _item(5, "返品理由", 600, 50),
        _item(6, "NOT E-", 50, 100, w=50, conf=0.3), _item(7, "1 0 41", 102, 100, w=40, conf=0.3),
        _item(8, "0 1", 200, 100, w=30),
        _item(10, '" 1 0 0', 440, 100, w=50, conf=0.5),
        _item(11, "サイズ違い", 600, 100),
    ]
    if with_qty:
        items.append(_item(9, "2", 330, 100, w=15))
    return items


def _propose(items):
    return propose_rows(items, config.TABLE_HEADERS, config.REVIEW_FIELDS, config.TABLE_END_LABELS, config.AUTO_ASSIGN_MIN_OVERLAP)


def test_propose_rows_returns_columns_and_bands():
    p = _propose(_page())
    assert p["status"] == "ok" and len(p["rows"]) == 1
    assert set(p["columns"]) == {"品番", "枝番", "数量", "単価", "返品理由"}
    assert p["columns"]["品番"][0] == 0.0 and p["columns"]["返品理由"][1] == 1e9
    assert p["bands"] == [[100, 120]]


def test_refine_replaces_text_and_merges_split_boxes():
    items = _page()
    p = _propose(items)
    # 読み直しの答え: 品番 → NOTE-104、枝番 → 01、数量 → 2、単価 → -100（この順で呼ばれる）
    reader = FakeReader([("NOTE-104", 0.95), ("0 1", 0.9), ("2", 0.99), ("-100", 0.8)])
    orf.refine_cells(IMAGE, items, p, reader)
    by_no = {it["no"]: it for it in items}
    assert by_no[6]["text"] == "NOTE-104" and by_no[6]["refined"] is True and by_no[6]["confidence"] == 0.95
    assert by_no[7]["text"] == ""  # 2つ目の枠は空にして1つ目にまとめる
    assert by_no[8]["text"] == "01"  # 空白は除く
    assert by_no[10]["text"] == "-100"
    assert by_no[11]["text"] == "サイズ違い"  # 返品理由は読み直さない
    assert [c["allowlist"] for c in reader.calls] == [
        config.REFINE_ALLOWLIST["品番"], config.REFINE_ALLOWLIST["枝番"], config.REFINE_ALLOWLIST["数量"], config.REFINE_ALLOWLIST["単価"]
    ]
    assert all(c["shape"][0] >= config.REFINE_TARGET_HEIGHT for c in reader.calls)  # 小さい欄は拡大してから読む


def test_refine_keeps_original_when_reread_is_empty():
    items = _page()
    p = _propose(items)
    reader = FakeReader([("", 0.0), ("", 0.0), ("", 0.0), ("", 0.0)])
    orf.refine_cells(IMAGE, items, p, reader)
    by_no = {it["no"]: it for it in items}
    assert by_no[6]["text"] == "NOT E-" and "refined" not in by_no[6]


def test_refine_reads_unassigned_cell_from_column_and_row_position():
    items = _page(with_qty=False)
    p = _propose(items)
    assert p["rows"][0]["cells"]["数量"]["ocr_nos"] == []
    reader = FakeReader([("NOTE-104", 0.9), ("01", 0.9), ("2", 0.7), ("-100", 0.9)])
    orf.refine_cells(IMAGE, items, p, reader)
    cell = p["rows"][0]["cells"]["数量"]
    assert len(cell["ocr_nos"]) == 1 and cell["assign"] == "auto"
    new = next(it for it in items if it["no"] == cell["ocr_nos"][0])
    assert new["text"] == "2" and new["refined"] is True
    assert p["columns"]["数量"][0] < new["box"]["x"] < p["columns"]["数量"][1]


def test_refine_skips_blank_cell():
    """何も書いていない欄は読み直さない（罫線のかけらを「3」と読まないため）。"""
    items = _page(with_qty=False)
    p = _propose(items)
    blank = np.full((400, 800, 3), 255, dtype=np.uint8)
    reader = FakeReader([("NOTE-104", 0.9), ("01", 0.9), ("3", 0.99), ("-100", 0.9)])
    orf.refine_cells(blank, items, p, reader)
    assert p["rows"][0]["cells"]["数量"]["ocr_nos"] == []
    assert len(reader.calls) == 3  # 数量は読み直しを呼ばない


def test_refine_ignores_low_confidence_reread_of_unassigned_cell():
    items = _page(with_qty=False)
    p = _propose(items)
    reader = FakeReader([("NOTE-104", 0.9), ("01", 0.9), ("7", config.REFINE_MIN_CONF_NEW - 0.1), ("-100", 0.9)])
    n = len(items)
    orf.refine_cells(IMAGE, items, p, reader)
    assert p["rows"][0]["cells"]["数量"]["ocr_nos"] == [] and len(items) == n


def test_value_start_splits_label_and_value_at_the_space():
    """「見出し 値」の画像: 見出し（x 0〜80）と値（x 100〜160）の間の空白で切る。文字の間の小さな隙間では切らない。"""
    crop = np.full((20, 170, 3), 255, dtype=np.uint8)
    crop[3:17, 2:80] = 0  # 見出し
    for x in range(100, 160, 10):  # 値（文字の間に 2px の隙間）
        crop[3:17, x : x + 8] = 0
    assert orf.value_start(crop, value_len=6) == 100
    # 見出しの中にも空白がある（「取引先コー」「ド」）ときは、右側の幅が値の文字数に近いほうで切る
    crop2 = np.full((20, 200, 3), 255, dtype=np.uint8)
    crop2[3:17, 2:60] = 0
    crop2[3:17, 70:90] = 0  # 「ド」
    for x in range(110, 170, 10):
        crop2[3:17, x : x + 8] = 0
    assert orf.value_start(crop2, value_len=6) == 110
    # 値が無い（右に文字が無い）→ None
    assert orf.value_start(crop[:, :90], value_len=6) is None


def test_refine_header_reads_value_part_only():
    items = [
        _item(1, "伝票番号", 400, 20), _item(2, "R- 0 0 0 1 2 3", 500, 20, w=120, conf=0.6),
        _item(3, "取引先コード IOOOO1", 50, 60, w=200),  # 見出しと値が同じ枠 → 右側の値だけ読み直す
        _item(4, "返品日", 400, 60), _item(5, "2 0 2 6 / 1 1 / 0 2", 500, 60, w=140, conf=0.4),
    ]
    reader = FakeReader([("100001", 0.9), ("R-000123", 0.9), ("2026/11/02", 0.95)])
    orf.refine_header(IMAGE, items, reader)
    by_no = {it["no"]: it for it in items}
    assert by_no[2]["text"] == "R-000123" and by_no[5]["text"] == "2026/11/02"
    assert by_no[3]["text"] == "取引先コード 100001"  # 見出しの文字は残す
    assert [c["allowlist"] for c in reader.calls] == [
        config.REFINE_HEADER_ALLOWLIST["会社コード"], config.REFINE_HEADER_ALLOWLIST["伝票番号"], config.REFINE_HEADER_ALLOWLIST["返品日"]
    ]
