"""OCRの精度を測るための伝票（架空）を、正解つきでたくさん作る。

使い方（return_slip_ocr フォルダで）:
    .venv\\Scripts\\python tools\\make_benchmark.py            → records\\benchmark\\ に伝票PDFと正解（cases.json）
    .venv\\Scripts\\python tools\\make_benchmark.py --count 20

同じ乱数の種（--seed）なら、毎回同じ伝票ができる。だから「直す前」と「直した後」を同じ伝票で比べられる。

伝票の種類（variant）
  clean … パソコンで作ったきれいなPDF（文字がそのまま入っている）
  scan  … 紙をスキャンしたような画像PDF（少し傾き・にじみ・ノイズ・JPEG圧縮）
  small … 文字が小さい（8pt）きれいなPDF
"""
import argparse
import io
import json
import os
import random
import sys

import cv2
import numpy as np
import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.make_samples import ITEMS, PARTNERS, REASONS  # noqa: E402

OUT = os.path.join(ROOT, "records", "benchmark")
VARIANTS = ("clean", "scan", "small")

# 実際の伝票でよく使われるフォント（Windows に入っているもの）。無ければ PyMuPDF 内蔵の "japan" を使う
FONT_FILES = [
    r"C:\Windows\Fonts\msgothic.ttc",
    r"C:\Windows\Fonts\meiryo.ttc",
    r"C:\Windows\Fonts\YuGothM.ttc",
    r"C:\Windows\Fonts\BIZ-UDGothicR.ttc",
    r"C:\Windows\Fonts\msmincho.ttc",
]


def _money(n):
    """1200 → "1,200"（伝票の表記）。マイナスは "-100"。"""
    return f"{n:,}"


def make_case(rng, idx):
    """1枚分の伝票の内容（正解）を作る。"""
    partner = rng.choice(PARTNERS)
    n_lines = rng.randint(2, 5)
    lines = []
    for _ in range(n_lines):
        item = rng.choice(ITEMS)
        qty = rng.choice([1, 2, 3, 5, 10, 12, 20, 24, 30])
        price = rng.choice([80, 98, 120, 150, 240, 250, 300, 540, 980, 1200, 1500, 2480])
        lines.append(
            {
                "品番": item[0],
                "枝番": item[1],
                "品名": item[2],
                "数量": str(qty),
                "単価": _money(price),
                "金額": _money(qty * price),
                "返品理由": rng.choice(REASONS)[1],
            }
        )
    if rng.random() < 0.4:
        disc = rng.choice([50, 100, 200, 500])
        lines.append({"品番": "", "枝番": "", "品名": "値引", "数量": "1", "単価": f"-{disc}", "金額": f"-{disc}", "返品理由": ""})
    total = sum(int(l["金額"].replace(",", "")) for l in lines)
    day = rng.randint(1, 28)
    fonts = [f for f in FONT_FILES if os.path.exists(f)]
    return {
        "font": rng.choice(fonts) if fonts else None,
        "header": {
            "会社名": partner[1],
            "会社コード": partner[0],
            "伝票番号": f"R-{rng.randint(1, 999999):06d}",
            "返品日": f"2026/11/{day:02d}",
            "元売上番号": f"S-{rng.randint(10000, 99999)}",
        },
        "lines": lines,
        "total": _money(total),
    }


def draw_slip(case, size=10):
    """伝票の内容から、文字がそのまま入ったPDF（bytes）を作る。tools/make_samples.py の伝票と同じ形。"""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    h = case["header"]
    if case.get("font"):
        page.insert_font(fontname="slip", fontfile=case["font"])
        font = pymupdf.Font(fontfile=case["font"])
        fname = "slip"
    else:
        font = pymupdf.Font("japan")
        fname = "japan"

    def width(s, sz):
        return font.text_length(s, fontsize=sz)

    def t(x, y, s, sz=None):
        page.insert_text((x, y), s, fontname=fname, fontsize=sz or size)

    def t_right(x_right, y, s):
        """数値の列は右寄せ（実際の伝票と同じ）"""
        page.insert_text((x_right - width(s, size), y), s, fontname=fname, fontsize=size)

    def fit(s, max_w):
        """列の幅に入らない品名は切る（実際の伝票でも省略される）"""
        while s and width(s, size) > max_w:
            s = s[:-1]
        return s

    t(230, 70, "返 品 伝 票", 20)
    t(40, 115, f"{h['会社名']} 御中", 12)
    t(40, 133, f"取引先コード {h['会社コード']}")
    t(380, 110, f"伝票番号   {h['伝票番号']}")
    t(380, 128, f"返品日     {h['返品日']}")
    t(380, 146, f"元伝票番号 {h['元売上番号']}")
    cols = [("No", 44), ("品番", 72), ("枝番", 158), ("品名", 200), ("数量", 330), ("単価", 378), ("金額", 432), ("返品理由", 490)]
    borders = [38, 64, 150, 192, 322, 370, 424, 482, 560]
    y0 = 190
    n = len(case["lines"])
    page.draw_rect(pymupdf.Rect(38, y0 - 16, 560, y0 + 6), color=(0, 0, 0), width=0.6)
    for x in borders:
        page.draw_line((x, y0 - 16), (x, y0 + 6 + 26 * n), color=(0.4, 0.4, 0.4), width=0.5)
    for name, x in cols:
        t(x, y0, name)
    y = y0 + 26
    for i, ln in enumerate(case["lines"], start=1):
        t(44, y, str(i))
        t(72, y, ln["品番"])
        t(158, y, ln["枝番"])
        t(200, y, fit(ln["品名"], 322 - 200 - 6))
        t_right(364, y, ln["数量"])
        t_right(418, y, ln["単価"])
        t_right(476, y, ln["金額"])
        t(490, y, ln["返品理由"])
        page.draw_line((38, y + 8), (560, y + 8), color=(0.6, 0.6, 0.6), width=0.4)
        y += 26
    t(370, y + 10, "合計（税込）")
    t_right(476, y + 10, case["total"])
    t(40, y + 40, "※この伝票は試験用の架空データです。", 9)
    data = doc.tobytes()
    doc.close()
    return data


def scan_like(pdf_bytes, rng, dpi=150):
    """きれいなPDFを、スキャナで読んだような画像だけのPDFにする。"""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        w_pt, h_pt = doc[0].rect.width, doc[0].rect.height
    # 少し傾ける（紙をまっすぐ置けなかった）
    angle = rng.uniform(-0.8, 0.8)
    m = cv2.getRotationMatrix2D((img.shape[1] / 2, img.shape[0] / 2), angle, 1.0)
    img = cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), borderValue=255)
    # にじみ・薄さ・ノイズ
    img = cv2.GaussianBlur(img, (3, 3), rng.uniform(0.5, 1.0))
    img = cv2.convertScaleAbs(img, alpha=rng.uniform(0.85, 1.0), beta=rng.uniform(5, 30))
    noise = rng.normalvariate(0, 1)  # 種から決まる強さ
    img = np.clip(img.astype(np.int16) + np.random.default_rng(int(abs(noise) * 1000)).normal(0, 8, img.shape), 0, 255).astype(np.uint8)
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
    assert ok
    out = pymupdf.open()
    page = out.new_page(width=w_pt, height=h_pt)
    page.insert_image(page.rect, stream=jpg.tobytes())
    data = out.tobytes()
    out.close()
    return data


def build(count, seed, out_dir=OUT, variants=VARIANTS):
    """伝票PDFと正解（cases.json）を out_dir に作り、cases（辞書のリスト）を返す。"""
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(seed)
    cases = []
    for i in range(1, count + 1):
        case = make_case(rng, i)
        for v in variants:
            if v == "clean":
                data = draw_slip(case)
            elif v == "small":
                data = draw_slip(case, size=8)
            else:
                data = scan_like(draw_slip(case), rng)
            name = f"bench_{i:02d}_{v}.pdf"
            with open(os.path.join(out_dir, name), "wb") as f:
                f.write(data)
            cases.append({"pdf": name, "variant": v, "no": i, **case})
    with open(os.path.join(out_dir, "cases.json"), "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
    return cases


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=10, help="伝票の枚数（種類ごと）")
    ap.add_argument("--seed", type=int, default=2026, help="乱数の種")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    cases = build(a.count, a.seed, a.out)
    print(f"作成しました: {a.out}（{len(cases)} ファイル）")


if __name__ == "__main__":
    main()
