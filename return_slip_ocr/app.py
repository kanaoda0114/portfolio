"""返品伝票OCR（結果確認・精度記録用）ローカルWebアプリ。

流れ: PDFアップロード → ページを画像化（PyMuPDF）→ EasyOCR → 文字・位置・信頼度
      → 明細の欄に枠を割り当て → 人が正解を入力 → 一致/不一致を判定・集計 → records/ にCSV保存
起動: .venv\\Scripts\\python.exe app.py  →  http://127.0.0.1:5000
"""
import base64
import logging
import time
import traceback

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

import config
from services import review as rv
from services.doc_store import DocStore, now_str
from services.errors import AppError
from services.ocr_engine import get_reader, reader_info, run_ocr
from services.pdf_renderer import inspect_pdf, render_first_page, render_page
from services.table_mapper import empty_cell, propose_rows

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
app.json.ensure_ascii = False
log = logging.getLogger("ocr_app")
store = DocStore(config.RECORDS_DIR)


@app.get("/")
def index():
    return render_template(
        "index.html",
        max_mb=config.MAX_UPLOAD_MB,
        low_conf=config.LOW_CONFIDENCE,
        fields=config.REVIEW_FIELDS,
    )


def _read_pdf_upload():
    file = request.files.get("pdf")
    if file is None or file.filename == "":
        raise AppError("PDFファイルが選択されていません。", "「PDFを選択」からファイルを選んでください。", code="no_file")
    if not file.filename.lower().endswith(".pdf"):
        raise AppError(
            f"PDF以外のファイルです: {file.filename}",
            "拡張子が .pdf のファイルを選んでください。",
            code="bad_extension",
        )
    return file.filename, file.read()


def _ocr_summary(items):
    confs = [it["confidence"] for it in items]
    return {
        "count": len(items),
        "avg_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "low_confidence_count": sum(c < config.LOW_CONFIDENCE for c in confs),
        "low_confidence_threshold": config.LOW_CONFIDENCE,
    }


@app.post("/api/ocr")
def api_ocr():
    """1ページ目だけをOCRする（最初の版のAPI。記録は残さない）。"""
    filename, pdf_bytes = _read_pdf_upload()

    t0 = time.perf_counter()
    image, png_bytes, meta = render_first_page(pdf_bytes, config.RENDER_DPI, config.MAX_RENDER_PIXELS)
    t1 = time.perf_counter()
    reader = get_reader(config.OCR_LANGS, config.USE_GPU, config.MODEL_DIR, config.ALLOW_MODEL_DOWNLOAD)
    t2 = time.perf_counter()
    items = run_ocr(image, reader)
    t3 = time.perf_counter()

    return jsonify(
        {
            "ok": True,
            "filename": filename,
            "file_size": len(pdf_bytes),
            "page_count": meta["page_count"],
            "ocr_page": 1,
            "image": {
                "width": meta["width"],
                "height": meta["height"],
                "dpi": meta["dpi"],
                "data_url": "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii"),
            },
            "items": items,
            "summary": _ocr_summary(items),
            "timing_sec": {
                "render": round(t1 - t0, 2),
                "model_load": round(t2 - t1, 2),
                "ocr": round(t3 - t2, 2),
            },
            "engine": reader_info(),
        }
    )


# ---------------------------------------------------------------------------
# 精度記録用 API（ページごとのOCR → 明細の正解入力 → 判定・集計・CSV保存）
# ---------------------------------------------------------------------------


def _judged_by_page(doc, review):
    out = {}
    for key, pr in review["pages"].items():
        p = int(key)
        ocr = store.load_ocr(doc, p)
        out[p] = rv.judge_rows(pr["rows"], ocr["items"] if ocr else [], config.REVIEW_FIELDS, config.NUMERIC_FIELDS)
    return out


def _summary(doc, review):
    return rv.summarize(
        _judged_by_page(doc, review), config.REVIEW_FIELDS, doc["page_count"], store.ocr_done_pages(doc)
    )


def _write_csvs(doc, review):
    judged = _judged_by_page(doc, review)
    saved_at = {int(k): v.get("saved_at", "") for k, v in review["pages"].items()}
    store.write_csv(
        doc, "review_cells.csv", rv.cells_csv(doc["filename"], judged, config.REVIEW_FIELDS, saved_at)
    )
    summary = rv.summarize(judged, config.REVIEW_FIELDS, doc["page_count"], store.ocr_done_pages(doc))
    store.write_csv(doc, "summary.csv", rv.summary_csv(doc["filename"], summary))


def _doc_payload(doc):
    review = store.load_review(doc)
    return {
        "ok": True,
        "doc_id": doc["id"],
        "filename": doc["filename"],
        "page_count": doc["page_count"],
        "records_folder": doc["folder"],
        "summary": _summary(doc, review),
    }


def _page_payload(doc, page, png_bytes, meta, ocr, source_override=None):
    review = store.load_review(doc)
    saved = review["pages"].get(str(page))
    items = ocr["items"] if ocr else []
    proposal = None
    if ocr:
        proposal = propose_rows(
            items, config.TABLE_HEADERS, config.REVIEW_FIELDS, config.TABLE_END_LABELS, config.AUTO_ASSIGN_MIN_OVERLAP
        )

    if saved:
        rows, source = saved["rows"], "saved"
    elif proposal:
        rows, source = proposal["rows"], "auto"
    else:
        rows, source = [], "none"

    return {
        "ok": True,
        "doc_id": doc["id"],
        "filename": doc["filename"],
        "page": page,
        "page_count": doc["page_count"],
        "image": {
            "width": meta["width"],
            "height": meta["height"],
            "dpi": meta["dpi"],
            "data_url": "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii"),
        },
        "ocr": None
        if not ocr
        else {
            "items": items,
            "summary": _ocr_summary(items),
            "timing_sec": ocr.get("timing_sec", {}),
            "engine": ocr.get("engine", {}),
            "ocr_at": ocr.get("ocr_at", ""),
        },
        "table": {
            "source": source_override or source,
            "saved_at": saved["saved_at"] if saved else "",
            "note": proposal["note"] if proposal else "",
            "unassigned": proposal["unassigned"] if proposal else [],
            "rows": rv.judge_rows(rows, items, config.REVIEW_FIELDS, config.NUMERIC_FIELDS),
        },
    }


@app.post("/api/docs")
def api_upload_doc():
    filename, pdf_bytes = _read_pdf_upload()
    page_count = inspect_pdf(pdf_bytes)
    doc = store.add(pdf_bytes, filename, page_count)
    return jsonify(_doc_payload(doc))


@app.get("/api/docs/<doc_id>")
def api_doc(doc_id):
    return jsonify(_doc_payload(store.get(doc_id)))


@app.get("/api/docs/<doc_id>/pages/<int:page>")
def api_page(doc_id, page):
    """ページ画像と、あればOCR結果・確認記録を返す（OCRは実行しない）。"""
    doc = store.get(doc_id)
    ocr = store.load_ocr(doc, page) if 1 <= page <= doc["page_count"] else None
    # OCRしたときと同じ解像度で画像化しないと、枠の位置がずれる
    dpi = ocr["dpi"] if ocr else config.RENDER_DPI
    _, png_bytes, meta = render_page(doc["bytes"], page, dpi, config.MAX_RENDER_PIXELS)
    return jsonify(_page_payload(doc, page, png_bytes, meta, ocr))


@app.post("/api/docs/<doc_id>/pages/<int:page>/ocr")
def api_page_ocr(doc_id, page):
    doc = store.get(doc_id)
    t0 = time.perf_counter()
    image, png_bytes, meta = render_page(doc["bytes"], page, config.RENDER_DPI, config.MAX_RENDER_PIXELS)
    t1 = time.perf_counter()
    reader = get_reader(config.OCR_LANGS, config.USE_GPU, config.MODEL_DIR, config.ALLOW_MODEL_DOWNLOAD)
    t2 = time.perf_counter()
    items = run_ocr(image, reader)
    t3 = time.perf_counter()

    ocr = {
        "page": page,
        "dpi": meta["dpi"],
        "width": meta["width"],
        "height": meta["height"],
        "ocr_at": now_str(),
        "timing_sec": {"render": round(t1 - t0, 2), "model_load": round(t2 - t1, 2), "ocr": round(t3 - t2, 2)},
        "engine": reader_info(),
        "items": items,
    }
    store.save_ocr(doc, page, ocr)

    # OCRし直した場合: 枠番号が変わるので、保存済みの枠の割り当ては自動割り当てに戻す。
    # 人が入力した正解は行・項目ごとにそのまま残す。
    review = store.load_review(doc)
    saved = review["pages"].get(str(page))
    source_override = None
    if saved:
        proposal = propose_rows(
            items, config.TABLE_HEADERS, config.REVIEW_FIELDS, config.TABLE_END_LABELS, config.AUTO_ASSIGN_MIN_OVERLAP
        )
        new_rows = []
        n = max(len(saved["rows"]), len(proposal["rows"]))
        for i in range(n):
            old = saved["rows"][i] if i < len(saved["rows"]) else None
            new = proposal["rows"][i] if i < len(proposal["rows"]) else None
            cells = {}
            for f in config.REVIEW_FIELDS:
                c = dict(new["cells"][f]) if new else empty_cell()
                if old and f in old["cells"]:
                    c["truth"] = old["cells"][f].get("truth", "")
                    c["truth_blank"] = old["cells"][f].get("truth_blank", False)
                cells[f] = c
            new_rows.append({"no_text": new["no_text"] if new else (old or {}).get("no_text", ""), "cells": cells})
        store.save_page_review(doc, page, new_rows, _write_csvs)
        source_override = "reocr"

    return jsonify(_page_payload(doc, page, png_bytes, meta, ocr, source_override))


def _parse_rows(body, items):
    if not isinstance(body, dict) or not isinstance(body.get("rows"), list):
        raise AppError("送信データの形式が正しくありません（rows がありません）。", code="bad_request")
    if len(body["rows"]) > 200:
        raise AppError("行が多すぎます（最大200行）。", code="bad_request")
    valid_nos = {it["no"] for it in items}
    rows = []
    for i, r in enumerate(body["rows"], start=1):
        cells_in = (r or {}).get("cells") or {}
        cells = {}
        for f in config.REVIEW_FIELDS:
            c = cells_in.get(f) or {}
            nos = c.get("ocr_nos") or []
            if not isinstance(nos, list) or not all(isinstance(n, int) for n in nos):
                raise AppError(f"{i}行目「{f}」の枠番号が不正です。", code="bad_request")
            bad = [n for n in nos if n not in valid_nos]
            if bad:
                raise AppError(
                    f"{i}行目「{f}」に、このページに無い枠番号 {bad} が指定されています。",
                    "ページを開き直してから、枠を選び直してください。",
                    code="bad_request",
                )
            truth = c.get("truth") or ""
            if not isinstance(truth, str) or len(truth) > 200:
                raise AppError(f"{i}行目「{f}」の正解が長すぎるか、文字列ではありません。", code="bad_request")
            cells[f] = {
                "ocr_nos": list(dict.fromkeys(nos)),
                "assign": "auto" if c.get("assign") == "auto" else "manual",
                "truth": truth,
                "truth_blank": bool(c.get("truth_blank")),
            }
        no_text = (r or {}).get("no_text") or ""
        rows.append({"no_text": str(no_text)[:20], "cells": cells})
    return rows


@app.put("/api/docs/<doc_id>/pages/<int:page>/review")
def api_save_review(doc_id, page):
    doc = store.get(doc_id)
    if not 1 <= page <= doc["page_count"]:
        raise AppError(f"{page}ページ目はありません。", status=404, code="page_out_of_range")
    ocr = store.load_ocr(doc, page)
    if not ocr:
        raise AppError(f"{page}ページ目はまだOCRしていません。", "先に「このページをOCR」を押してください。", code="not_ocr")

    rows = _parse_rows(request.get_json(silent=True), ocr["items"])
    review = store.save_page_review(doc, page, rows, _write_csvs)
    saved = review["pages"][str(page)]
    return jsonify(
        {
            "ok": True,
            "page": page,
            "saved_at": saved["saved_at"],
            "rows": rv.judge_rows(rows, ocr["items"], config.REVIEW_FIELDS, config.NUMERIC_FIELDS),
            "summary": _summary(doc, review),
            "records_folder": doc["folder"],
        }
    )


@app.errorhandler(AppError)
def handle_app_error(e):
    log.warning("AppError[%s]: %s", e.code, e.message)
    return jsonify(e.to_dict()), e.status


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    err = AppError(
        f"ファイルサイズが上限（{config.MAX_UPLOAD_MB}MB）を超えています。",
        "config.py の MAX_UPLOAD_MB を増やすか、小さいPDFを使ってください。",
        status=413,
        code="too_large",
    )
    return jsonify(err.to_dict()), 413


@app.errorhandler(Exception)
def handle_unexpected(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    err = AppError(
        f"想定外のエラーが発生しました（{type(e).__name__}: {e}）",
        "起動したコマンドプロンプトに詳細（Traceback）が出ています。",
        status=500,
        code="unexpected",
    )
    return jsonify(err.to_dict()), 500


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"起動中: http://{config.HOST}:{config.PORT}  （終了は Ctrl+C）")
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)
