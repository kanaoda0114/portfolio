"""アップロードされたPDFと、その記録ファイル（records フォルダ）を管理する。

- PDF本体も記録フォルダに保存する（original.pdf）ので、サーバーを再起動しても一覧に戻る
- 記録は PDF の内容（SHA-256）ごとのフォルダに保存するので、
  同じPDFをアップロードし直すと、OCR結果と確認済みの正解がそのまま戻る

records/<PDF名>_<ハッシュ先頭8桁>/
    original.pdf     …  読み込んだPDF本体
    doc.json         …  ファイル名・ページ数・読み込んだ日時
    ocr/page_01.json …  ページごとのOCR結果（文字・位置・信頼度）
    review.json      …  明細の割り当てと、人が確認した正解（アプリが読み込む元データ）
    header.json      …  会社名・会社コード・伝票番号・返品日・元売上番号（OCRの候補を人が確認して保存）
    status.json      …  伝票の状態（確認中／確定済み／出力済み）と日時
    review_cells.csv …  全欄の OCR結果／正解／判定（見直し用）
    summary.csv      …  項目別・ページ別の集計
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime

from .errors import AppError


STATE_REVIEWING = "確認中"
STATE_CONFIRMED = "確定済み"
STATE_EXPORTED = "出力済み"


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_stem(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", stem).strip(" .")
    return stem[:60] or "document"


def _write_atomic(path, text, encoding="utf-8"):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except PermissionError:
        raise AppError(
            f"ファイルに書き込めません: {os.path.basename(path)}",
            "そのファイルをExcelなどで開いている場合は閉じてから、もう一度保存してください。",
            status=409,
            code="file_locked",
        )
    except OSError as e:
        raise AppError(f"記録の保存に失敗しました（{e}）", status=500, code="save_failed")


class DocStore:
    def __init__(self, records_dir):
        self.records_dir = records_dir
        self._docs = {}
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._restore()

    def _restore(self):
        """前回までに読み込んだPDF（records/<PDF>/original.pdf）を、起動時に一覧へ戻す。"""
        if not os.path.isdir(self.records_dir):
            return
        for name in sorted(os.listdir(self.records_dir)):
            folder = os.path.join(self.records_dir, name)
            path = os.path.join(folder, "original.pdf")
            meta = os.path.join(folder, "doc.json")
            if not (os.path.isfile(path) and os.path.isfile(meta)):
                continue
            try:
                with open(meta, encoding="utf-8") as f:
                    info = json.load(f)
                with open(path, "rb") as f:
                    data = f.read()
            except (OSError, ValueError):
                continue
            doc = {"id": info["id"], "filename": info["filename"], "page_count": info["page_count"], "bytes": data, "folder": folder, "added_at": info.get("added_at", "")}
            self._docs[doc["id"]] = doc

    def add(self, pdf_bytes, filename, page_count):
        doc_id = hashlib.sha256(pdf_bytes).hexdigest()[:16]
        with self._lock:
            existing = self._docs.get(doc_id)
        if existing is not None:
            return existing  # 同じPDFをもう一度読み込んだ：前回の記録をそのまま使う
        folder = os.path.join(self.records_dir, f"{_safe_stem(filename)}_{doc_id[:8]}")
        try:
            os.makedirs(os.path.join(folder, "ocr"), exist_ok=True)
            # アプリを再起動しても一覧に残るように、PDF本体と情報を保存する
            with open(os.path.join(folder, "original.pdf"), "wb") as f:
                f.write(pdf_bytes)
            _write_atomic(
                os.path.join(folder, "doc.json"),
                json.dumps({"id": doc_id, "filename": filename, "page_count": page_count, "added_at": now_str()}, ensure_ascii=False),
            )
        except OSError as e:
            raise AppError(f"記録フォルダを作れません: {folder}（{e}）", status=500, code="records_dir")
        doc = {"id": doc_id, "filename": filename, "page_count": page_count, "bytes": pdf_bytes, "folder": folder, "added_at": now_str()}
        with self._lock:
            self._docs[doc_id] = doc
        return doc

    def remove(self, doc):
        """一覧から外す（記録フォルダは残す。確定済み・出力済みの記録を守るため）。"""
        with self._lock:
            self._docs.pop(doc["id"], None)
        for name in ("original.pdf", "doc.json"):
            try:
                os.remove(os.path.join(doc["folder"], name))
            except OSError:
                pass

    def get(self, doc_id):
        with self._lock:
            doc = self._docs.get(doc_id)
        if doc is None:
            raise AppError(
                "このPDFはサーバー上にありません（サーバーを再起動した可能性があります）。",
                "PDFをもう一度アップロードしてください。記録は残っているので、続きから確認できます。",
                status=404,
                code="doc_not_found",
            )
        return doc

    # ---- OCR結果 ----

    def _ocr_path(self, doc, page):
        return os.path.join(doc["folder"], "ocr", f"page_{page:02d}.json")

    def load_ocr(self, doc, page):
        path = self._ocr_path(doc, page)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            raise AppError(
                f"{page}ページ目のOCR記録を読めません（{e}）",
                f"{path} を削除して、そのページをOCRし直してください。",
                status=500,
                code="ocr_cache_broken",
            )

    def save_ocr(self, doc, page, data):
        _write_atomic(self._ocr_path(doc, page), json.dumps(data, ensure_ascii=False, indent=1))

    def ocr_done_pages(self, doc):
        return {p for p in range(1, doc["page_count"] + 1) if os.path.exists(self._ocr_path(doc, p))}

    # ---- 確認結果 ----

    def _review_path(self, doc):
        return os.path.join(doc["folder"], "review.json")

    def load_review(self, doc):
        path = self._review_path(doc)
        if not os.path.exists(path):
            return {"filename": doc["filename"], "page_count": doc["page_count"], "pages": {}}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            raise AppError(
                f"確認記録（review.json）を読めません（{e}）",
                f"{path} が壊れている可能性があります。別名で退避してから再度開いてください。",
                status=500,
                code="review_broken",
            )

    def save_page_review(self, doc, page, rows, write_csvs):
        """1ページ分の確認結果を保存し、CSVも作り直す。"""
        with self._save_lock:
            review = self.load_review(doc)
            review["filename"] = doc["filename"]
            review["pages"][str(page)] = {"saved_at": now_str(), "rows": rows}
            review["updated_at"] = now_str()
            _write_atomic(self._review_path(doc), json.dumps(review, ensure_ascii=False, indent=1))
            write_csvs(doc, review)
            return review

    # ---- 伝票ヘッダ（伝票番号・返品日・取引先名） ----

    def _json_path(self, doc, name):
        return os.path.join(doc["folder"], name)

    def _load_json(self, doc, name, default):
        path = self._json_path(doc, name)
        if not os.path.exists(path):
            return default
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            raise AppError(f"{name} を読めません（{e}）", f"{path} が壊れている可能性があります。", status=500, code="json_broken")

    def load_header(self, doc):
        return self._load_json(doc, "header.json", {"会社名": "", "会社コード": "", "伝票番号": "", "返品日": "", "元売上番号": ""})

    def save_header(self, doc, header):
        _write_atomic(self._json_path(doc, "header.json"), json.dumps(header, ensure_ascii=False, indent=1))

    # ---- 伝票の状態（確認中 → 確定済み → 出力済み） ----

    def load_status(self, doc):
        return self._load_json(doc, "status.json", {"state": STATE_REVIEWING})

    def save_status(self, doc, status):
        _write_atomic(self._json_path(doc, "status.json"), json.dumps(status, ensure_ascii=False, indent=1))

    def ensure_editable(self, doc):
        """確定済み・出力済みの伝票は編集させない（要件定義書 F7）。"""
        state = self.load_status(doc)["state"]
        if state != STATE_REVIEWING:
            raise AppError(
                f"この伝票は「{state}」のため編集できません。",
                "直すときは「確定を取り消す」を押してください（出力済みの伝票は取り消せません）。",
                status=409,
                code="locked",
            )

    def all_docs(self):
        with self._lock:
            return list(self._docs.values())

    def write_csv(self, doc, name, text):
        # BOM付きUTF-8: Excelでダブルクリックしても文字化けしない
        _write_atomic(os.path.join(doc["folder"], name), text, encoding="utf-8-sig")
