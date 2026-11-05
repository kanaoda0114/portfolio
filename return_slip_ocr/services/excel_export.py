"""販売管理システム取込用Excelの作成と、出力履歴（要件定義書 F5）。

列の並び・列名・コード値は import_format.json で決める（コードは書き換えない）。
"""
import csv
import io
import json
import os
from datetime import datetime
from decimal import Decimal

from openpyxl import Workbook

from .errors import AppError

HISTORY_COLUMNS = ["出力日時", "取引先コード", "伝票番号", "明細行数", "出力ファイル名", "元PDFファイル名"]


def load_format(path):
    try:
        with open(path, encoding="utf-8") as f:
            fmt = json.load(f)
    except (OSError, ValueError) as e:
        raise AppError(f"取込フォーマット設定を読めません（{e}）", f"{path} を確認してください。", status=500, code="format_broken")
    if not fmt.get("columns"):
        raise AppError("取込フォーマット設定に列（columns）がありません。", status=500, code="format_broken")
    return fmt


def _number(v):
    """Decimal などの数値を、Excel に入れやすい int / float にする。"""
    if v is None or v == "":
        return None
    d = Decimal(str(v))
    return int(d) if d == d.to_integral_value() else float(d)


def _ymd(iso):
    """'2026-10-01' → '2026/10/01'（無ければ空）"""
    return (iso or "").replace("-", "/")


def slip_records(slip, filename, confirmed_at):
    """build_return() の結果を、列の値を引ける辞書のリストにする（明細1行＝1件）。

    import_format.json の "source" は、ここで作る辞書のキーを指す。
    """
    h = slip["header"]
    out = []
    for i, line in enumerate(slip["lines"], start=1):
        sales = line.get("元売上") or {}
        out.append(
            {
                **h,
                **line,
                "行番号": i,
                "計上月": _ymd(h.get("fiscal_month")),
                "締め日": _ymd(h.get("closing_date")),
                # 明細ごとに照合した元の売上（値引の行は無い）。見出しの「元売上番号」（OCRの候補）は上書きする
                "元売上番号": sales.get("sales_no") or "",
                "売上日": _ymd(sales.get("sales_date")),
                "売上単価": sales.get("unit_price"),
                "元ファイル名": filename,
                "確定日時": confirmed_at,
            }
        )
    return out


def build_table(records, fmt):
    """見出し行と、データ行（列の型に合わせた値）を作る。"""
    cols = fmt["columns"]
    head = [c["name"] for c in cols]
    rows = []
    for rec in records:
        row = []
        for c in cols:
            if "fixed" in c:
                v = c["fixed"]
            else:
                v = rec.get(c["source"])
                if "codes" in c and v not in (None, ""):
                    v = c["codes"].get(v, v)
            if c.get("type") == "number":
                v = _number(v) if v is not None else None
            elif c.get("type") == "date":
                v = datetime.strptime(v, "%Y/%m/%d").date() if v else None
            else:
                v = "" if v is None else str(v)
            row.append(v)
        rows.append(row)
    return head, rows


def write_xlsx(path, fmt, head, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = fmt.get("sheet_name") or "Sheet1"
    ws.append(head)
    for r in rows:
        ws.append(r)
    # 「=」で始まる文字は、そのままだとExcelが数式として実行してしまう（数式インジェクション）。
    # 伝票の文字は外から来たデータなので、文字の列は必ず「ただの文字」として書き込む。
    for row_cells in ws.iter_rows(min_row=1):
        for cell in row_cells:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    # 文字の列は「文字列」書式にする（品番 01 や伝票番号が数値に化けないように）
    for ci, c in enumerate(fmt["columns"], start=1):
        letter = ws.cell(row=1, column=ci).column_letter
        if c.get("type") == "date":
            fmt_code = "yyyy/mm/dd"
        elif c.get("type") == "number":
            fmt_code = "General"
        else:
            fmt_code = "@"
        for cell in ws[letter][1:]:
            cell.number_format = fmt_code
        ws.column_dimensions[letter].width = max(10, len(c["name"]) * 2 + 2)
    tmp = path + ".tmp"
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    except PermissionError:
        raise AppError(
            f"Excelファイルを保存できません: {os.path.basename(path)}",
            "同じ名前のファイルをExcelで開いている場合は閉じてから、もう一度出力してください。",
            status=409,
            code="file_locked",
        )


def export_file_name(now=None):
    return f"返品取込_{(now or datetime.now()).strftime('%Y%m%d_%H%M%S')}.xlsx"


# ---- 出力履歴 ----


def load_history(path):
    """出力履歴の各行（辞書）を返す。ファイルが無ければ空。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def history_keys(path):
    return {(r.get("取引先コード", ""), r.get("伝票番号", "")) for r in load_history(path)}


def append_history(path, entries):
    """出力した伝票を履歴に追記する（BOM付きUTF-8: Excelで開いても文字化けしない）。"""
    new = not os.path.exists(path)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HISTORY_COLUMNS, lineterminator="\r\n")
    if new:
        w.writeheader()
    for e in entries:
        w.writerow({k: e.get(k, "") for k in HISTORY_COLUMNS})
    try:
        with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            f.write(buf.getvalue())
    except PermissionError:
        raise AppError(
            "出力履歴（export_history.csv）に書き込めません。",
            "Excelなどで開いている場合は閉じてから、もう一度出力してください。",
            status=409,
            code="file_locked",
        )
