"""自動テストを実行し、結果をExcelのテスト結果表（テスト証跡）にする。

使い方（return_slip_ocr フォルダで）:
    .venv\\Scripts\\python tools\\test_report.py
    → docs\\test_reports\\テスト結果_YYYYMMDD_HHMMSS.xlsx ができる

シート:
    サマリ   … 実施日時・件数・合格/不合格・対象のコミット
    テスト明細 … 1テスト1行（分類・テスト項目・条件・結果・エラー内容）
"""
import ast
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(ROOT, "tests")
OUT_DIR = os.path.join(ROOT, "docs", "test_reports")

# テストファイルごとの対象（要件定義書の番号）
FILE_TARGETS = {
    "test_slip_check": "入力チェック（F4）・出す値の決め方（F6）",
    "test_excel_export": "取込用Excelの出力（F5）",
    "test_api": "確定（F7）・出力（F5）・出力先の設定・セキュリティ",
}

HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
OK_FILL = PatternFill("solid", fgColor="E2EFDA")
NG_FILL = PatternFill("solid", fgColor="F8CBAD")
THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def section_map(path):
    """テストファイルの「# ---- 分類 ----」見出しと docstring を、テスト関数ごとに引けるようにする。"""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    heads = [(i + 1, m.group(1).strip()) for i, ln in enumerate(lines) if (m := re.match(r"#\s*-{2,}\s*(.+?)\s*-{2,}", ln))]
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            sec = [h for ln, h in heads if ln < node.lineno]
            out[node.name] = {"section": sec[-1] if sec else "基本", "doc": (ast.get_docstring(node) or "").strip()}
    return out


def run_pytest(xml_path):
    cmd = [sys.executable, "-m", "pytest", "-q", f"--junitxml={xml_path}"]
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")


def git(*args):
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        return r.stdout.strip()
    except OSError:
        return ""


def parse_results(xml_path):
    sections = {}
    for name in os.listdir(TEST_DIR):
        if name.startswith("test_") and name.endswith(".py"):
            sections[name[:-3]] = section_map(os.path.join(TEST_DIR, name))

    rows = []
    for tc in ET.parse(xml_path).getroot().iter("testcase"):
        module = tc.get("classname", "").split(".")[-1]
        full = tc.get("name", "")
        m = re.match(r"(test_[^\[]+)(?:\[(.*)\])?$", full)
        func, param = (m.group(1), m.group(2) or "") if m else (full, "")
        info = sections.get(module, {}).get(func, {"section": "", "doc": ""})

        title = func[len("test_"):]
        plan_no = ""
        pm = re.match(r"(\d{2})_(.*)", title)
        if pm:
            plan_no, title = pm.group(1), pm.group(2)
        title = title.replace("_", " ")

        failure = tc.find("failure")
        error = tc.find("error")
        skipped = tc.find("skipped")
        if failure is not None or error is not None:
            result, detail = "NG", ((failure if failure is not None else error).get("message") or "")[:300]
        elif skipped is not None:
            result, detail = "対象外", skipped.get("message") or ""
        else:
            result, detail = "OK", ""
        rows.append(
            {
                "対象": FILE_TARGETS.get(module, module),
                "分類": info["section"],
                "計画No": plan_no,
                "テスト項目": title,
                "条件": param,
                "補足": info["doc"],
                "結果": result,
                "時間(秒)": round(float(tc.get("time", 0)), 3),
                "エラー内容": detail,
                "テスト関数": f"{module}.py::{full}",
            }
        )
    return rows


def style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def write_report(rows, out_path, started, commit, dirty, py_output):
    wb = Workbook()

    # ---- サマリ ----
    ws = wb.active
    ws.title = "サマリ"
    ok = sum(r["結果"] == "OK" for r in rows)
    ng = sum(r["結果"] == "NG" for r in rows)
    skip = sum(r["結果"] == "対象外" for r in rows)
    summary = [
        ("テスト結果表", ""),
        ("対象", "返品伝票OCR（return_slip_ocr）"),
        ("実施日時", started.strftime("%Y/%m/%d %H:%M:%S")),
        ("対象コミット", commit + ("（未コミットの変更あり）" if dirty else "")),
        ("実行方法", "pytest による自動テスト（tools/test_report.py で出力）"),
        ("件数", len(rows)),
        ("合格（OK）", ok),
        ("不合格（NG）", ng),
        ("対象外", skip),
        ("判定", "合格" if ng == 0 and rows else "不合格"),
        ("pytest の出力", py_output),
    ]
    for r in summary:
        ws.append(r)
    ws["A1"].font = Font(bold=True, size=14)
    for i in range(2, len(summary) + 1):
        ws.cell(row=i, column=1).font = Font(bold=True)
        for c in (1, 2):
            ws.cell(row=i, column=c).border = BORDER
            ws.cell(row=i, column=c).alignment = Alignment(vertical="top", wrap_text=True)
    judge = ws.cell(row=10, column=2)
    judge.fill = OK_FILL if ng == 0 else NG_FILL
    judge.font = Font(bold=True)
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 80

    # ---- テスト明細 ----
    ws = wb.create_sheet("テスト明細")
    cols = ["No", "対象", "分類", "計画No", "テスト項目", "条件", "補足", "結果", "時間(秒)", "エラー内容", "テスト関数"]
    widths = [5, 26, 18, 7, 46, 18, 40, 7, 8, 40, 50]
    ws.append(cols)
    style_header(ws, 1, len(cols))
    for i, r in enumerate(rows, start=1):
        ws.append([i] + [r[c] for c in cols[1:]])
        for c in range(1, len(cols) + 1):
            cell = ws.cell(row=i + 1, column=c)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(cell.value, str):
                cell.data_type = "s"  # 「=」で始まっても数式にしない
        res = ws.cell(row=i + 1, column=cols.index("結果") + 1)
        res.fill = OK_FILL if r["結果"] == "OK" else NG_FILL if r["結果"] == "NG" else PatternFill()
        res.alignment = Alignment(horizontal="center", vertical="top")
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{len(rows) + 1}"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    wb.save(out_path)


def main():
    started = datetime.now()
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = os.path.join(tmp, "result.xml")
        proc = run_pytest(xml_path)
        if not os.path.exists(xml_path):
            print("テストを実行できませんでした。\n" + proc.stdout + proc.stderr)
            return 1
        rows = parse_results(xml_path)

    last = (proc.stdout.strip().splitlines() or [""])[-1]
    commit = git("log", "-1", "--format=%h %s") or "（不明）"
    dirty = bool(git("status", "--porcelain"))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(OUT_DIR, f"テスト結果_{started.strftime('%Y%m%d_%H%M%S')}.xlsx")
    write_report(rows, out, started, commit, dirty, last)

    ng = sum(r["結果"] == "NG" for r in rows)
    print(f"{len(rows)}件中 OK {len(rows) - ng}件 / NG {ng}件")
    print(f"テスト結果表: {out}")
    return 0 if ng == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
