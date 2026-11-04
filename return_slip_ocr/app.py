"""返品伝票OCR（結果確認・精度記録用）ローカルWebアプリ。

流れ: PDFアップロード → ページを画像化（PyMuPDF）→ EasyOCR → 文字・位置・信頼度
      → 明細の欄に枠を割り当て → 人が正解を入力 → 一致/不一致を判定・集計 → records/ にCSV保存
起動: .venv\\Scripts\\python.exe app.py  →  http://127.0.0.1:5000
"""
import base64
import io
import json
import logging
import os
import time
import traceback

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

import config
from services import db as dbm
from services import excel_export as xl
from services import master_import as mi
from services import review as rv
from services import slip_check as sc
from services.doc_store import STATE_CONFIRMED, STATE_EXPORTED, STATE_REVIEWING, DocStore, now_str
from services.errors import AppError
from services.ocr_engine import get_reader, reader_info, run_ocr
from services.pdf_renderer import inspect_pdf, render_first_page, render_page
from services.table_mapper import empty_cell, propose_rows

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
app.json.ensure_ascii = False
log = logging.getLogger("ocr_app")
store = DocStore(config.RECORDS_DIR)


_ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}


@app.before_request
def block_foreign_requests():
    """このPCのブラウザで開いたこのアプリの画面以外からの操作を断る。

    - Host の確認: 悪意のあるサイトが自分のドメインを 127.0.0.1 に向ける攻撃（DNSリバインディング）を防ぐ
    - Origin の確認: ほかのサイトを開いているときに、裏でこのアプリを操作される攻撃（CSRF）を防ぐ
    """
    if request.host not in _ALLOWED_HOSTS:
        return jsonify({"ok": False, "code": "bad_host", "error": "このアドレスからは利用できません。"}), 403
    if request.method in ("POST", "PUT", "DELETE"):
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") not in {f"http://{h}" for h in _ALLOWED_HOSTS}:
            return jsonify({"ok": False, "code": "bad_origin", "error": "ほかのサイトからの操作は受け付けません。"}), 403
    return None


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
        "header": store.load_header(doc),
        "status": store.load_status(doc),
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
    store.ensure_editable(doc)
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
    # 人が入力した正解と明細の追加項目は行・項目ごとにそのまま残す。
    # 「OCR結果を採用」は読み取り内容が変わるので外す（新しい結果を人が見直す）。
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
            new_rows.append(
                {
                    "no_text": new["no_text"] if new else (old or {}).get("no_text", ""),
                    "cells": cells,
                    "extra": rv.empty_extra((old or {}).get("extra")),
                }
            )
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
                "adopt_ocr": bool(c.get("adopt_ocr")),
            }
        extra_in = (r or {}).get("extra") or {}
        if not isinstance(extra_in, dict):
            raise AppError(f"{i}行目の追加項目（extra）の形式が正しくありません。", code="bad_request")
        reason = extra_in.get("返品理由") or ""
        if not isinstance(reason, str) or len(reason) > 40:
            raise AppError(f"{i}行目の返品理由が長すぎます（40文字まで）。", code="bad_request")
        if extra_in.get("明細区分", "通常") not in rv.LINE_KINDS:
            raise AppError(f"{i}行目の明細区分が不正です。", code="bad_request")
        if extra_in.get("良品不良", "") not in rv.CONDITIONS:
            raise AppError(f"{i}行目の良品／不良の値が不正です。", code="bad_request")
        no_text = (r or {}).get("no_text") or ""
        rows.append({"no_text": str(no_text)[:20], "cells": cells, "extra": rv.empty_extra(extra_in)})
    return rows


@app.put("/api/docs/<doc_id>/pages/<int:page>/review")
def api_save_review(doc_id, page):
    doc = store.get(doc_id)
    store.ensure_editable(doc)
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


# ---------------------------------------------------------------------------
# 確定と取込用Excelの出力（docs/要件定義書.md F4〜F7）
# ---------------------------------------------------------------------------


def _history_path():
    return config.HISTORY_PATH


def _load_settings():
    if not os.path.exists(config.SETTINGS_PATH):
        return {}
    try:
        with open(config.SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        raise AppError(
            f"設定ファイルを読めません（{e}）",
            f"{config.SETTINGS_PATH} を削除すると、初期設定に戻ります。",
            status=500,
            code="settings_broken",
        )


def _export_dir():
    """利用者が設定した出力先。未設定なら初期値。"""
    return _load_settings().get("export_dir") or config.DEFAULT_EXPORT_DIR


def _prepare_export_dir(path):
    """出力先として使えるようにする。フォルダが無ければ作る。

    戻り値: 利用者に見せるお知らせ（作成したとき）。何もしなければ ""。
    使えなければ AppError（理由と対処方法つき）。
    """
    if not path:
        raise AppError("出力先フォルダが入力されていません。", code="bad_export_dir")
    if not os.path.isabs(path):
        raise AppError(
            f"出力先は「C:\\返品取込」のような、ドライブから始まるフォルダの場所で入力してください（入力された値: {path}）",
            "エクスプローラーでフォルダを開き、上のアドレス欄をコピーして貼り付けると確実です。",
            code="bad_export_dir",
        )
    drive = os.path.splitdrive(path)[0]
    if drive and not os.path.exists(drive + os.sep):
        raise AppError(
            f"ドライブ「{drive}」が見つかりません（出力先: {path}）",
            "USBメモリやネットワークドライブの場合は、つながっているか確認してください。",
            code="bad_export_dir",
        )
    notice = ""
    if os.path.exists(path) and not os.path.isdir(path):
        raise AppError(
            f"同じ名前のファイルがあるため、フォルダとして使えません: {path}",
            "別のフォルダ名を指定してください。",
            code="bad_export_dir",
        )
    if not os.path.isdir(path):
        try:
            os.makedirs(path)
        except OSError as e:
            raise AppError(
                f"出力先フォルダが無かったので作成しようとしましたが、作成できませんでした: {path}（{e}）",
                "書き込み権限のある場所（例: ドキュメントの中）を指定するか、フォルダを手で作ってから設定してください。",
                code="bad_export_dir",
            )
        notice = f"出力先フォルダが無かったので作成しました: {path}"
    probe = os.path.join(path, f".write_test_{os.getpid()}.tmp")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
    except OSError as e:
        raise AppError(
            f"出力先フォルダに書き込めません: {path}（{e}）",
            "書き込み権限のあるフォルダを指定してください。",
            code="bad_export_dir",
        )
    return notice


def _settings_payload(notice=""):
    s = _load_settings()
    return {
        "ok": True,
        "export_dir": _export_dir(),
        "export_dir_is_default": not s.get("export_dir"),
        "default_export_dir": config.DEFAULT_EXPORT_DIR,
        "notice": notice,
    }


@app.get("/api/settings")
def api_get_settings():
    return jsonify(_settings_payload())


@app.put("/api/settings")
def api_save_settings():
    """出力先フォルダを保存する（要件定義書 F5）。空にすると初期値に戻す。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("export_dir", ""), str):
        raise AppError("送信データの形式が正しくありません。", code="bad_request")
    path = body.get("export_dir", "").strip().strip('"')
    settings = _load_settings()
    notice = ""
    if path:
        path = os.path.normpath(path)
        notice = _prepare_export_dir(path)
        settings["export_dir"] = path
    else:
        settings.pop("export_dir", None)
    try:
        os.makedirs(os.path.dirname(config.SETTINGS_PATH), exist_ok=True)
        with open(config.SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=1)
    except OSError as e:
        raise AppError(f"設定を保存できません（{e}）", status=500, code="settings_save_failed")
    return jsonify(_settings_payload(notice))


def _load_master():
    try:
        return sc.PartnerMaster.load(config.PARTNER_MASTER_PATH)
    except OSError as e:
        raise AppError(
            f"取引先マスタを読めません（{e}）",
            f"{config.PARTNER_MASTER_PATH} があるか確認してください。",
            status=500,
            code="master_missing",
        )


def _check(doc, master=None, history=None):
    """伝票1枚分のチェック結果（F4）と出力行を作る。"""
    review = store.load_review(doc)
    return sc.build_slip(
        store.load_header(doc),
        _judged_by_page(doc, review),
        config.REVIEW_FIELDS,
        config.NUMERIC_FIELDS,
        master or _load_master(),
        config.AMOUNT_TOLERANCE,
        xl.history_keys(_history_path()) if history is None else history,
    )


def _unreviewed_pages(doc):
    """OCRまたは確認（保存）が済んでいないページ番号。"""
    review = store.load_review(doc)
    done_ocr = store.ocr_done_pages(doc)
    return [p for p in range(1, doc["page_count"] + 1) if p not in done_ocr or str(p) not in review["pages"]]


@app.put("/api/docs/<doc_id>/header")
def api_save_header(doc_id):
    doc = store.get(doc_id)
    store.ensure_editable(doc)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise AppError("送信データの形式が正しくありません。", code="bad_request")
    header = {}
    for k in sc.HEADER_FIELDS:
        v = body.get(k) or ""
        if not isinstance(v, str) or len(v) > 40:
            raise AppError(f"{k}が長すぎるか、文字列ではありません（40文字まで）。", code="bad_request")
        header[k] = v.strip()
    store.save_header(doc, header)
    return jsonify({"ok": True, "header": header, "check": _check(doc)})


@app.get("/api/docs/<doc_id>/check")
def api_check(doc_id):
    doc = store.get(doc_id)
    return jsonify({"ok": True, "check": _check(doc), "unreviewed_pages": _unreviewed_pages(doc)})


@app.post("/api/docs/<doc_id>/confirm")
def api_confirm(doc_id):
    """伝票を確定する（F7）。エラーがあれば確定しない。"""
    doc = store.get(doc_id)
    store.ensure_editable(doc)
    pending = _unreviewed_pages(doc)
    if pending:
        raise AppError(
            f"OCRまたは確認が済んでいないページがあります（{', '.join(map(str, pending))}ページ目）。",
            "全ページで「保存して判定」を押してから確定してください。",
            code="pages_pending",
        )
    check = _check(doc)
    if check["errors"]:
        return jsonify(
            {
                "ok": False,
                "code": "check_failed",
                "error": f"入力チェックでエラーが {check['errors']} 件あるため、確定できません。",
                "hint": "エラーの内容を直してから、もう一度確定してください。",
                "check": check,
            }
        ), 400
    status = {"state": STATE_CONFIRMED, "confirmed_at": now_str()}
    store.save_status(doc, status)
    return jsonify({"ok": True, "status": status, "check": check})


@app.delete("/api/docs/<doc_id>/confirm")
def api_unconfirm(doc_id):
    """確定を取り消す（F7）。出力済みの伝票は取り消せない。"""
    doc = store.get(doc_id)
    state = store.load_status(doc)["state"]
    if state == STATE_EXPORTED:
        raise AppError(
            "出力済みの伝票は、確定を取り消せません。",
            "取込済みのデータとずれないよう、販売管理システム側で修正してください。",
            status=409,
            code="already_exported",
        )
    status = {"state": STATE_REVIEWING}
    store.save_status(doc, status)
    return jsonify({"ok": True, "status": status})


@app.post("/api/export")
def api_export():
    """確定済みの伝票をまとめて1つのExcelに出力する（F5）。"""
    body = request.get_json(silent=True) or {}
    doc_ids = body.get("doc_ids")
    if not isinstance(doc_ids, list) or not doc_ids:
        doc_ids = [d["id"] for d in store.all_docs()]

    master = _load_master()
    history = xl.history_keys(_history_path())
    fmt = xl.load_format(config.IMPORT_FORMAT_PATH)

    exported, skipped, warnings, records = [], [], [], []
    for doc_id in dict.fromkeys(doc_ids):
        doc = store.get(doc_id)
        status = store.load_status(doc)
        if status["state"] != STATE_CONFIRMED:
            reason = "出力済みです" if status["state"] == STATE_EXPORTED else "確定していません"
            skipped.append({"filename": doc["filename"], "reason": reason})
            continue
        check = _check(doc, master, history)
        if check["errors"]:
            msgs = [i["message"] for i in check["issues"] if i["level"] == sc.ERROR]
            skipped.append({"filename": doc["filename"], "reason": " / ".join(msgs)})
            continue
        for i in check["issues"]:
            if i["level"] == sc.WARNING:
                warnings.append({"filename": doc["filename"], **i})
        records.extend(xl.slip_records(check, doc["filename"], status["confirmed_at"].replace("-", "/")))
        exported.append((doc, check))

    if not exported:
        return jsonify(
            {
                "ok": False,
                "code": "nothing_to_export",
                "error": "出力できる伝票がありません。",
                "hint": "伝票を確定してから出力してください。",
                "skipped": skipped,
            }
        ), 400

    # 初回（records/exports がまだ無い）や、設定したあとでフォルダが消された場合は作り直す。
    # ドライブが無い（ネットワークドライブが切れている等）ときは出力しない。
    export_dir = _export_dir()
    notice = _prepare_export_dir(export_dir)

    head, rows = xl.build_table(records, fmt)
    name = xl.export_file_name()
    path = os.path.join(export_dir, name)
    xl.write_xlsx(path, fmt, head, rows)

    exported_at = now_str()
    xl.append_history(
        _history_path(),
        [
            {
                "出力日時": exported_at.replace("-", "/"),
                "取引先コード": c["header"]["取引先コード"],
                "伝票番号": c["header"]["伝票番号"],
                "明細行数": len(c["lines"]),
                "出力ファイル名": name,
                "元PDFファイル名": d["filename"],
            }
            for d, c in exported
        ],
    )
    for d, _ in exported:
        st = store.load_status(d)
        store.save_status(d, {**st, "state": STATE_EXPORTED, "exported_at": exported_at, "export_file": name})

    return jsonify(
        {
            "ok": True,
            "file": name,
            "path": path,
            "exported": [d["filename"] for d, _ in exported],
            "rows": len(rows),
            "skipped": skipped,
            "warnings": warnings,
            "notice": notice,
        }
    )


# ---------------------------------------------------------------------------
# マスタ・売上データの取り込み（docs/要件定義書.md F8、docs/データ設計書.md 4章）
# ---------------------------------------------------------------------------


def _db():
    return dbm.connect(config.DB_PATH)


@app.get("/api/import/kinds")
def api_import_kinds():
    """取り込めるデータの種類と、今の件数。"""
    con = _db()
    try:
        counts = dbm.counts(con)
    finally:
        con.close()
    return jsonify(
        {
            "ok": True,
            "kinds": [{"kind": k, "label": d.label, "columns": [c.name for c in d.cols]} for k, d in mi.DATASETS.items()],
            "counts": counts,
        }
    )


@app.get("/api/import/<kind>/template")
def api_import_template(kind):
    """取り込み用の見本Excel（列名と説明だけ）をダウンロードする。"""
    if kind not in mi.DATASETS:
        raise AppError(f"取り込みの種類が正しくありません: {kind}", status=404, code="bad_kind")
    return send_file(
        io.BytesIO(mi.template_xlsx(kind)),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"{mi.DATASETS[kind].label}_見本.xlsx",
    )


@app.post("/api/import/<kind>")
def api_import(kind):
    """Excel / CSV を取り込む。1行でもエラーがあれば何も入れず、エラーの一覧を返す。"""
    file = request.files.get("file")
    if file is None or file.filename == "":
        raise AppError("ファイルが選択されていません。", "取り込むExcelまたはCSVを選んでください。", code="no_file")
    data = file.read()
    if len(data) > config.MAX_IMPORT_MB * 1024 * 1024:
        raise AppError(f"ファイルが大きすぎます（上限 {config.MAX_IMPORT_MB}MB）。", code="too_large")
    con = _db()
    try:
        result = mi.import_file(con, kind, file.filename, data, now_str())
        result["counts"] = dbm.counts(con)
    finally:
        con.close()
    status = 200 if result["imported"] else 400
    if not result["imported"]:
        result.update(
            {
                "ok": False,
                "code": "import_rejected",
                "error": f"{result['label']}にエラーが {result['error_count']} 件あるため、何も取り込んでいません。",
                "hint": "エラーの行を直してから、もう一度取り込んでください。",
            }
        )
    else:
        result["ok"] = True
    return jsonify(result), status


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
