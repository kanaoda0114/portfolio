"""ブラウザを使わずに OCR だけ確認するコマンド。

使い方: py check_ocr.py "C:\\path\\to\\sample.pdf"
"""
import sys
import time

import config
from services.errors import AppError
from services.ocr_engine import get_reader, run_ocr
from services.pdf_renderer import render_first_page


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    try:
        with open(path, "rb") as f:
            data = f.read()
        image, _, meta = render_first_page(data, config.RENDER_DPI, config.MAX_RENDER_PIXELS)
        print(f"ページ数: {meta['page_count']}  1ページ目画像: {meta['width']}x{meta['height']} ({meta['dpi']}dpi)")
        t = time.perf_counter()
        reader = get_reader(config.OCR_LANGS, config.USE_GPU, config.MODEL_DIR, config.ALLOW_MODEL_DOWNLOAD)
        items = run_ocr(image, reader)
        print(f"検出数: {len(items)}  処理時間: {time.perf_counter() - t:.1f}秒\n")
    except FileNotFoundError:
        print(f"エラー: ファイルが見つかりません: {path}")
        return 1
    except AppError as e:
        print(f"エラー: {e.message}\n対処: {e.hint}")
        return 1

    for it in items:
        b = it["box"]
        mark = " ※低" if it["confidence"] < config.LOW_CONFIDENCE else ""
        print(f"{it['no']:>3}  conf={it['confidence']:.3f}{mark}  x={b['x']:.0f} y={b['y']:.0f} w={b['w']:.0f} h={b['h']:.0f}  {it['text']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
