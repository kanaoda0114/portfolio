"""試すための架空データを作る（会社名・商品・金額はすべて架空）。

使い方（return_slip_ocr フォルダで）:
    .venv\\Scripts\\python tools\\make_samples.py
    → samples\\ に次のファイルができる
       取引先マスタ.xlsx / 商品マスタ.xlsx / 返品理由マスタ.xlsx / 売上データ.xlsx（取り込み用）
       返品伝票_サンプル.pdf（OCRで読み取る伝票）
"""
import io
import os
import sys

import pymupdf
from openpyxl import load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.master_import import template_xlsx  # noqa: E402

OUT = os.path.join(ROOT, "samples")

PARTNERS = [
    ["100001", "株式会社サンプル商事", "カブシキガイシャサンプルショウジ", "サンプル商事;(株)サンプル商事", "T1234567890123", 20, "翌月末", "切り捨て", 1],
    ["100002", "テスト物産株式会社", "テストブッサンカブシキガイシャ", "テスト物産", "T9876543210987", "末", "翌々月10日", "四捨五入", 1],
]
ITEMS = [
    ["NOTE-104", "01", "A5ノート 横罫", "冊", 10, 1],
    ["PEN-2203", "12", "ゲルインクペン 黒", "本", 10, 1],
    ["FILE-0551", "03", "クリアファイル A4", "枚", 10, 1],
    ["TAPE-018", "", "マスキングテープ", "個", 10, 1],
    ["TEA-001", "", "緑茶ティーバッグ（来客用）", "箱", 8, 1],
]
REASONS = [
    ["01", "サイズ違い", "サイズ間違い", 1, 1],
    ["02", "色違い", "カラー違い", 2, 1],
    ["03", "初期不良", "不良品;破損", 3, 1],
    ["04", "数量違い", "数量間違い;過剰納品", 4, 1],
    ["05", "注文取消", "キャンセル", 5, 1],
    ["99", "その他", "", 99, 1],
]
SALES = [
    ["S-24001", "100001", "2026/10/01", 1, "NOTE-104", "01", 10, 240, None],
    ["S-24001", "100001", "2026/10/01", 2, "PEN-2203", "12", 30, 120, None],
    ["S-24015", "100001", "2026/10/20", 1, "FILE-0551", "03", 20, 80, None],
    ["S-24015", "100001", "2026/10/20", 2, "TAPE-018", "", 10, 300, None],
    ["S-24015", "100001", "2026/10/20", 3, "TEA-001", "", 5, 540, None],
    ["S-24102", "100001", "2026/10/28", 1, "NOTE-104", "01", 5, 250, None],
    ["S-24020", "100002", "2026/10/05", 1, "PEN-2203", "12", 50, 115, None],
]


def write_master(kind, name, rows):
    wb = load_workbook(io.BytesIO(template_xlsx(kind)))
    ws = wb.worksheets[0]
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.number_format == "@" and cell.value is not None:
                cell.value = str(cell.value)
    wb.save(os.path.join(OUT, name))


def write_slip_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)

    def t(x, y, s, size=10):
        page.insert_text((x, y), s, fontname="japan", fontsize=size)

    t(230, 70, "返 品 伝 票", 20)
    t(40, 115, "株式会社サンプル商事 御中", 12)
    t(40, 133, "取引先コード 100001", 10)
    t(380, 110, "伝票番号   R-000123", 10)
    t(380, 128, "返品日     2026/11/02", 10)
    t(380, 146, "元伝票番号 S-24001", 10)
    # 実際の伝票と同じように、列の間に縦線を引く（OCRが隣の列の文字とつなげて読まないように）
    cols = [("No", 44), ("品番", 72), ("枝番", 158), ("品名", 200), ("数量", 330), ("単価", 378), ("金額", 432), ("返品理由", 490)]
    borders = [38, 64, 150, 192, 322, 370, 424, 482, 560]
    y0 = 190
    page.draw_rect(pymupdf.Rect(38, y0 - 16, 560, y0 + 6), color=(0, 0, 0), width=0.6)
    for x in borders:
        page.draw_line((x, y0 - 16), (x, y0 + 6 + 26 * 3), color=(0.4, 0.4, 0.4), width=0.5)
    for n, x in cols:
        t(x, y0, n)
    rows = [
        ("1", "NOTE-104", "01", "A5ノート 横罫", "2", "240", "480", "サイズ違い"),
        ("2", "PEN-2203", "12", "ゲルインクペン 黒", "10", "120", "1,200", "初期不良"),
        ("3", "", "", "値引", "1", "-100", "-100", ""),
    ]
    y = y0 + 26
    for r in rows:
        for (n, x), v in zip(cols, r):
            t(x, y, v)
        page.draw_line((38, y + 8), (560, y + 8), color=(0.6, 0.6, 0.6), width=0.4)
        y += 26
    t(370, y + 10, "合計（税込）")
    t(440, y + 10, "1,580")
    t(40, y + 40, "※この伝票は試験用の架空データです。", 9)
    doc.save(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    write_master("partner", "取引先マスタ.xlsx", PARTNERS)
    write_master("item", "商品マスタ.xlsx", ITEMS)
    write_master("reason", "返品理由マスタ.xlsx", REASONS)
    write_master("sales", "売上データ.xlsx", SALES)
    write_slip_pdf(os.path.join(OUT, "返品伝票_サンプル.pdf"))
    print("作成しました:", OUT)


if __name__ == "__main__":
    main()
