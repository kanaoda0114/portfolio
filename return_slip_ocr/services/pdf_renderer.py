"""PDF のページを画像（numpy配列 + PNG）に変換する。

PyMuPDF を使うので Poppler などの外部ツールは不要。
"""
import numpy as np
import pymupdf as fitz

from .errors import AppError


def _open_pdf(pdf_bytes):
    if not pdf_bytes:
        raise AppError("ファイルが空です（0バイト）。", "別のPDFを選び直してください。", code="empty_file")

    # 拡張子だけ .pdf で中身が違うファイルを弾く
    if b"%PDF-" not in pdf_bytes[:1024]:
        raise AppError(
            "PDFファイルではありません（先頭に %PDF- の署名がありません）。",
            "拡張子だけ .pdf に変えた画像やExcelではないか確認してください。",
            code="not_pdf",
        )

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        raise AppError(
            f"PDFを開けませんでした（ファイルが壊れている可能性があります）: {e}",
            "PDFビューアで開けるか確認し、保存し直してから再度アップロードしてください。",
            code="pdf_broken",
        )

    if doc.needs_pass:
        doc.close()
        raise AppError(
            "パスワード付き（暗号化）PDFのため読み込めません。",
            "パスワードを解除したPDFを用意してください。",
            code="pdf_encrypted",
        )
    if doc.page_count == 0:
        doc.close()
        raise AppError("PDFにページがありません。", code="pdf_no_pages")
    return doc


def inspect_pdf(pdf_bytes):
    """PDFとして開けるか確認し、ページ数を返す。"""
    with _open_pdf(pdf_bytes) as doc:
        return doc.page_count


def render_page(pdf_bytes, page_no, dpi, max_pixels):
    """指定ページ（1始まり）を RGB 画像にする。

    戻り値: (image: np.ndarray[h, w, 3], png_bytes: bytes, meta: dict)
    """
    with _open_pdf(pdf_bytes) as doc:
        if not 1 <= page_no <= doc.page_count:
            raise AppError(
                f"{page_no}ページ目はありません（全{doc.page_count}ページ）。",
                code="page_out_of_range",
                status=404,
            )

        page = doc.load_page(page_no - 1)
        rect = page.rect
        scale = dpi / 72
        expected = int(rect.width * scale) * int(rect.height * scale)
        if expected > max_pixels:
            raise AppError(
                f"{page_no}ページ目が大きすぎます（{rect.width:.0f}×{rect.height:.0f}pt、"
                f"{dpi}dpiで約{expected / 1_000_000:.0f}百万画素）。",
                "config.py の RENDER_DPI を下げてください。",
                code="page_too_large",
            )

        try:
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        except Exception as e:
            raise AppError(f"{page_no}ページ目の画像化に失敗しました: {e}", code="render_failed", status=500)

        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n).copy()
        png_bytes = pix.tobytes("png")
        meta = {
            "page_count": doc.page_count,
            "page": page_no,
            "width": pix.width,
            "height": pix.height,
            "dpi": dpi,
        }

    return image, png_bytes, meta


def render_first_page(pdf_bytes, dpi, max_pixels):
    return render_page(pdf_bytes, 1, dpi, max_pixels)
