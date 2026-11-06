"""OCRの正解率を測る（Issue #19）。

使い方（return_slip_ocr フォルダで）:
    .venv\\Scripts\\python tools\\ocr_benchmark.py --tag before
    .venv\\Scripts\\python tools\\ocr_benchmark.py --tag after --refine on
    .venv\\Scripts\\python tools\\ocr_benchmark.py --count 3 --variants clean,scan   （少しだけ試す）

流れ
  1. tools/make_benchmark.py で、正解つきの架空伝票を作る（同じ種なら毎回同じ伝票）
  2. アプリと同じ流れ（services/ocr_pipeline.py）で1枚ずつ読む
  3. 読んだ値と正解を、アプリの判定（services/review.py の judge）で比べる
     … 全角半角・空白・カンマなどの表記ゆれは「一致（表記差）」として正解に数える
  4. 項目ごとの正解率と、間違えた欄の一覧を表にして、docs/test_reports/ に保存する

「直す前」と「直した後」を同じ --count --seed で測れば、何がどれだけ良くなったかを数字で比べられる。
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
from services import review as rv  # noqa: E402
from services.header_extract import HEADER_FIELDS  # noqa: E402
from services.ocr_engine import get_reader  # noqa: E402
from services.ocr_pipeline import cell_texts, header_values, read_page  # noqa: E402
from services.pdf_renderer import render_first_page  # noqa: E402
from tools import make_benchmark as mb  # noqa: E402

REPORT_DIR = os.path.join(ROOT, "docs", "test_reports")
FIELDS = config.REVIEW_FIELDS


def _counter():
    return {"target": 0, "correct": 0, "exact": 0}


def _judge_into(cnt, field, ocr_text, truth, misses, where):
    numeric = field in config.NUMERIC_FIELDS or field in ("会社コード",)
    r = rv.judge(field, ocr_text, truth, truth == "", {field} if numeric else set())
    cnt["target"] += 1
    if r["result"] in (rv.MATCH, rv.MATCH_NOTATION):
        cnt["correct"] += 1
        if r["result"] == rv.MATCH:
            cnt["exact"] += 1
    else:
        misses.append({**where, "項目": field, "正解": truth, "OCR": ocr_text, "判定": r["result"]})
    return r


def run(cases, bench_dir, refine, known=None, log=print):
    reader = get_reader(config.OCR_LANGS, config.USE_GPU, config.MODEL_DIR, config.ALLOW_MODEL_DOWNLOAD)
    by_variant = {}  # variant -> {field: counter}
    misses = []
    row_count_errors = []
    times = []
    for i, case in enumerate(cases, start=1):
        v = case["variant"]
        cnts = by_variant.setdefault(v, {f: _counter() for f in list(HEADER_FIELDS) + FIELDS})
        with open(os.path.join(bench_dir, case["pdf"]), "rb") as f:
            data = f.read()
        image, _, _ = render_first_page(data, config.RENDER_DPI, config.MAX_RENDER_PIXELS)
        t0 = time.perf_counter()
        items, proposal = read_page(image, reader, refine=refine, known=known)
        times.append(time.perf_counter() - t0)
        where = {"伝票": case["pdf"]}

        hv = header_values(items)
        for f in HEADER_FIELDS:
            _judge_into(cnts[f], f, hv.get(f, ""), case["header"][f], misses, {**where, "行": "見出し"})

        read_rows = cell_texts(items, proposal["rows"])
        truth_rows = case["lines"]
        if len(read_rows) != len(truth_rows):
            row_count_errors.append({"伝票": case["pdf"], "正解の行数": len(truth_rows), "読んだ行数": len(read_rows), "状態": proposal["status"]})
        for r_i, truth in enumerate(truth_rows):
            got = read_rows[r_i] if r_i < len(read_rows) else {f: "" for f in FIELDS}
            for f in FIELDS:
                _judge_into(cnts[f], f, got[f], truth[f], misses, {**where, "行": r_i + 1})
        log(f"[{i}/{len(cases)}] {case['pdf']}  {times[-1]:.1f}秒  行数 {len(read_rows)}/{len(truth_rows)}")
    return {"by_variant": by_variant, "misses": misses, "row_count_errors": row_count_errors, "avg_sec": sum(times) / len(times) if times else 0}


def _pct(c):
    return f"{c['correct'] / c['target'] * 100:.1f}%" if c["target"] else "-"


def report_md(result, args, when):
    lines = [f"# OCR精度の計測結果（{args.tag}）", "", f"- 計測日時: {when}", f"- 伝票: {args.count}枚 × 種類 {args.variants}（種 {args.seed}）",
             f"- 欄の読み直し（REFINE_CELLS）: {'on' if args.refine_flag else 'off'}", f"- マスタの値で直す: {'on' if args.known_flag else 'off'}", f"- 1枚あたりの読み取り時間: {result['avg_sec']:.1f}秒", ""]
    variants = list(result["by_variant"])
    all_fields = list(HEADER_FIELDS) + FIELDS
    lines += ["## 項目ごとの正解率（表記ゆれは正解に数える）", "", "| 項目 | " + " | ".join(variants) + " | 全体 |", "|---|" + "---|" * (len(variants) + 1)]
    totals = {v: _counter() for v in variants}
    grand = _counter()
    for f in all_fields:
        cells = []
        fsum = _counter()
        for v in variants:
            c = result["by_variant"][v][f]
            cells.append(f"{_pct(c)} ({c['correct']}/{c['target']})")
            for k in fsum:
                fsum[k] += c[k]
                totals[v][k] += c[k]
                grand[k] += c[k]
        lines.append(f"| {f} | " + " | ".join(cells) + f" | {_pct(fsum)} ({fsum['correct']}/{fsum['target']}) |")
    lines.append("| **全項目** | " + " | ".join(f"**{_pct(totals[v])}**" for v in variants) + f" | **{_pct(grand)}** ({grand['correct']}/{grand['target']}) |")
    lines.append("")
    if result["row_count_errors"]:
        lines += ["## 行数が合わなかった伝票", "", "| 伝票 | 正解の行数 | 読んだ行数 | 状態 |", "|---|---|---|---|"]
        lines += [f"| {e['伝票']} | {e['正解の行数']} | {e['読んだ行数']} | {e['状態']} |" for e in result["row_count_errors"]]
        lines.append("")
    lines += [f"## 間違えた欄（{len(result['misses'])}件）", "", "| 伝票 | 行 | 項目 | 正解 | OCR | 判定 |", "|---|---|---|---|---|---|"]
    lines += [f"| {m['伝票']} | {m['行']} | {m['項目']} | `{m['正解']}` | `{m['OCR']}` | {m['判定']} |" for m in result["misses"]]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tag", default="run", help="結果ファイル名に付ける印（before / after など）")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--variants", default=",".join(mb.VARIANTS))
    ap.add_argument("--refine", choices=["on", "off", "config"], default="config", help="欄の読み直し。config なら config.REFINE_CELLS に従う")
    ap.add_argument("--bench-dir", default=mb.OUT)
    ap.add_argument("--known", choices=["on", "off"], default="on", help="マスタの値で直す（services/candidates.py）")
    a = ap.parse_args()
    a.refine_flag = {"on": True, "off": False, "config": config.REFINE_CELLS}[a.refine]
    a.known_flag = a.known == "on"

    variants = tuple(a.variants.split(","))
    cases = mb.build(a.count, a.seed, a.bench_dir, variants)
    print(f"伝票を作成: {len(cases)} ファイル → {a.bench_dir}")
    # マスタの値（架空のサンプルと同じ）。元売上番号は伝票ごとに乱数なので渡さない
    known = {"品番": [it[0] for it in mb.ITEMS], "返品理由": [r[1] for r in mb.REASONS]} if a.known_flag else None
    result = run(cases, a.bench_dir, a.refine_flag, known)

    when = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = report_md(result, a, when)
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(REPORT_DIR, f"OCR精度_{a.tag}_{stamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    with open(os.path.join(REPORT_DIR, f"OCR精度_{a.tag}_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({"args": vars(a), **result}, f, ensure_ascii=False, indent=1)
    print()
    print(md)
    print(f"保存: {path}")


if __name__ == "__main__":
    main()
