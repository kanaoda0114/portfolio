"""返品伝票1枚分を、マスタ・売上データと照合してチェックする（要件定義書 1.4版 F3・F4・F9）。

- 出す値は、人が入力した正解 → 採用したOCRの値 の順で決める（F6。slip_check.cell_value）
- マスタ・売上は照合に使うだけで、伝票の値を上書きしない
- 元の売上の候補が複数あるときは、人が選ぶ（勝手に1つに決めない）
"""
import calendar
import re
import unicodedata
from datetime import date
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

from .slip_check import ERROR, WARNING, cell_value, parse_date, parse_number, partner_key

LINE_FIELDS = ("品番", "枝番", "数量", "単価", "返品理由")
NUMERIC = {"数量", "単価"}
ROUNDING = {"切り捨て": ROUND_DOWN, "四捨五入": ROUND_HALF_UP, "切り上げ": ROUND_CEILING}


def _issue(level, check, message, page=None, row=None):
    return {"level": level, "check": check, "message": message, "page": page, "row": row}


def _norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _iso(d):
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# マスタを引く
# ---------------------------------------------------------------------------


def find_partner(con, code, name):
    """会社コード（無ければ会社名・別名）で取引先を探す。戻り値: (行, 問題のリスト)"""
    issues = []
    by_code = con.execute("SELECT * FROM m_partner WHERE partner_code = ?", (code,)).fetchone() if code else None
    by_name = None
    if name:
        key = partner_key(name)
        for row in con.execute("SELECT * FROM m_partner"):
            names = [row["name"]] + [a for a in (row["aliases"] or "").split(";") if a.strip()]
            if any(partner_key(n) == key for n in names):
                by_name = row
                break
    if code and by_code is None:
        issues.append(_issue(ERROR, "S1", f"会社コード「{code}」が取引先マスタにありません。"))
    if not code and name and by_name is None:
        issues.append(_issue(ERROR, "S1", f"会社名「{name}」が取引先マスタにありません（似た名前は選びません）。"))
    if by_code is not None and name and by_name is not None and by_code["partner_code"] != by_name["partner_code"]:
        issues.append(
            _issue(
                ERROR,
                "S2",
                f"会社コード {code}（{by_code['name']}）と会社名「{name}」が、取引先マスタでは別の会社です。",
            )
        )
    if by_code is not None and name and by_name is None:
        issues.append(
            _issue(WARNING, "S2", f"会社名「{name}」が、会社コード {code} の会社名（{by_code['name']}）・別名と一致しません。")
        )
    return (by_code or by_name), issues


def reason_index(con):
    """返品理由の文字 → 理由コード の対応（理由名と別の書き方）。"""
    idx = {}
    for row in con.execute("SELECT reason_code, name, aliases FROM m_return_reason WHERE active = 1"):
        for n in [row["name"]] + (row["aliases"] or "").split(";"):
            if n.strip():
                idx[_norm(n)] = (row["reason_code"], row["name"])
    return idx


def reason_choices(con):
    return [dict(r) for r in con.execute("SELECT reason_code, name FROM m_return_reason WHERE active = 1 ORDER BY sort_order")]


def find_item(con, item_no, branch_no):
    return con.execute("SELECT * FROM m_item WHERE item_no = ? AND branch_no = ?", (item_no, branch_no or "")).fetchone()


# ---------------------------------------------------------------------------
# 元の売上の候補
# ---------------------------------------------------------------------------


def returned_qty(con, sales_no, line_no, exclude_return_id=None):
    """その売上明細で、すでに確定している返品数量の合計（今の伝票を除く）。"""
    sql = "SELECT COALESCE(SUM(qty), 0) FROM t_return_line WHERE sales_no = ? AND sales_line_no = ?"
    args = [sales_no, line_no]
    if exclude_return_id is not None:
        sql += " AND return_id <> ?"
        args.append(exclude_return_id)
    return con.execute(sql, args).fetchone()[0]


def sales_candidates(con, partner_code, item_no, branch_no, return_date, sales_no=None, exclude_return_id=None):
    """返品の元になりうる売上明細を、新しい順に返す。

    sales_no（元売上番号）があればその売上だけ、無ければ返品日以前の同じ会社・品番・枝番の売上から探す。
    返品できる残りが0のものは、元売上番号の指定がないときは候補から外す。
    """
    sql = """SELECT s.sales_no, s.sales_date, s.partner_code, l.line_no, l.qty, l.unit_price
             FROM t_sales s JOIN t_sales_line l ON l.sales_no = s.sales_no
             WHERE l.item_no = ? AND l.branch_no = ?"""
    args = [item_no, branch_no or ""]
    if sales_no:
        sql += " AND s.sales_no = ?"
        args.append(sales_no)
    else:
        sql += " AND s.partner_code = ?"
        args.append(partner_code)
        if return_date:
            sql += " AND s.sales_date <= ?"
            args.append(return_date)
    sql += " ORDER BY s.sales_date DESC, s.sales_no DESC, l.line_no"
    out = []
    for r in con.execute(sql, args):
        remaining = r["qty"] - returned_qty(con, r["sales_no"], r["line_no"], exclude_return_id)
        if remaining <= 0 and not sales_no:
            continue
        out.append(
            {
                "sales_no": r["sales_no"],
                "line_no": r["line_no"],
                "sales_date": r["sales_date"],
                "partner_code": r["partner_code"],
                "qty": r["qty"],
                "unit_price": Decimal(str(r["unit_price"])),
                "remaining": remaining,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 計上月と締め日（Q10・Q14）
# ---------------------------------------------------------------------------


def closing_date_for(day, closing_day):
    """その日が入る締め期間の、締め日。closing_day=99 は末日。"""
    if closing_day == 99:
        return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])
    if day.day <= closing_day:
        return date(day.year, day.month, min(closing_day, calendar.monthrange(day.year, day.month)[1]))
    y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(y, m, min(closing_day, calendar.monthrange(y, m)[1]))


def fiscal(return_day, closing_day, confirm_day):
    """戻り値: {"fiscal_month": "YYYY-MM", "closing_date": "YYYY-MM-DD", "late": bool, "original_closing": ...}

    返品日の締め日を過ぎて確定するときは、確定日の締め期間に計上する（翌月の請求で相殺）。
    """
    original = closing_date_for(return_day, closing_day)
    late = confirm_day > original
    closing = closing_date_for(confirm_day, closing_day) if late else original
    return {
        "fiscal_month": closing.strftime("%Y-%m"),
        "closing_date": _iso(closing),
        "original_closing": _iso(original),
        "late": late,
    }


# ---------------------------------------------------------------------------
# 税率ごとの合計と消費税（要件定義書 F5・Q9・Q15）
# ---------------------------------------------------------------------------


def tax_summary(lines, rounding):
    """単価は税込（Q9）。税率ごとに税込金額を合計し、消費税を税率ごとに1回だけ端数処理する。

    値引の行の税率が「按分」のときは、通常の行の税率ごとの税込合計の比率で値引額を分ける（Q15）。
    1円未満の端数は、税込合計が大きいほうの税率に寄せる（分けた合計が元の値引額と必ず同じになる）。
    戻り値: {"rates": [{"rate", "items", "items_total", "discounts", "total", "tax", "tax_exact", "base"}], "total", "rounding"}
    """
    by_rate = {}
    for ln in lines:
        if ln.get("明細区分") == "通常" and ln.get("金額") is not None and ln.get("税率"):
            by_rate.setdefault(ln["税率"], {"items": [], "discounts": []})["items"].append(ln["金額"])

    items_total = {r: sum(v["items"]) for r, v in by_rate.items()}
    for ln in lines:
        if ln.get("明細区分") != "値引" or ln.get("金額") is None:
            continue
        mode = ln.get("税率の決め方")
        amount = ln["金額"]
        if mode in ("10", "8"):
            by_rate.setdefault(int(mode), {"items": [], "discounts": []})["discounts"].append({"amount": amount, "note": "値引"})
        elif mode == "按分":
            base_total = sum(items_total.values())
            rates = sorted(items_total, key=lambda r: items_total[r], reverse=True)
            if not rates or base_total == 0:
                continue
            shares, given = {}, 0
            for r in rates[1:]:  # 小さいほうの税率から、比率で切り捨てて割り当てる
                share = int(Decimal(amount) * Decimal(items_total[r]) / Decimal(base_total))
                shares[r] = share
                given += share
            shares[rates[0]] = amount - given  # 残り（端数を含む）は、合計が大きい税率へ
            for r, share in shares.items():
                by_rate[r]["discounts"].append(
                    {"amount": share, "note": f"値引の按分（{amount} × {items_total[r]} ÷ {base_total}）"}
                )

    out = []
    for rate in sorted(by_rate, reverse=True):
        v = by_rate[rate]
        total = sum(v["items"]) + sum(d["amount"] for d in v["discounts"])
        exact = Decimal(total) * rate / (100 + rate)
        tax = _round(exact, rounding)
        out.append(
            {
                "rate": rate,
                "items": v["items"],
                "items_total": sum(v["items"]),
                "discounts": v["discounts"],
                "total": total,
                "tax_exact": str(exact.quantize(Decimal("0.01"))),
                "tax": tax,
                "base": total - tax,
            }
        )
    return {"rates": out, "total": sum(r["total"] for r in out), "rounding": rounding}


# ---------------------------------------------------------------------------
# 伝票1枚のチェック
# ---------------------------------------------------------------------------


def _round(value, mode):
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUNDING.get(mode, ROUND_DOWN)))


def _is_empty_row(row):
    cells_empty = all(
        not (row["cells"][f].get("truth") or "").strip()
        and not row["cells"][f].get("truth_blank")
        and not (row["cells"][f].get("ocr_text") or "").strip()
        for f in LINE_FIELDS
        if f in row["cells"]
    )
    ex = row.get("extra") or {}
    return cells_empty and not ex.get("良品不良") and ex.get("明細区分", "通常") == "通常"


def build_return(con, header, judged_by_page, confirm_day, exclude_return_id=None):
    """伝票1枚分の照合とチェック。

    header         : {"会社名", "会社コード", "伝票番号", "返品日", "元売上番号"}（人が確定した値）
    judged_by_page : {ページ: review.judge_rows() の結果}（明細の欄は LINE_FIELDS）
    confirm_day    : 確定する日（締め日のチェックに使う）
    exclude_return_id: この伝票がすでに確定済みなら、その返品ID（残りの計算から自分を除く）
    """
    issues = []
    header = header or {}
    company = (header.get("会社名") or "").strip()
    code = _norm(header.get("会社コード"))
    slip_no = _norm(header.get("伝票番号"))
    sales_no_hint = _norm(header.get("元売上番号"))
    raw_date = header.get("返品日") or ""

    # ---- 伝票 ----
    if not company and not code:
        issues.append(_issue(ERROR, "C1", "会社名（または会社コード）が入力されていません。"))
    if not slip_no:
        issues.append(_issue(ERROR, "C1", "伝票番号が入力されていません。"))
    ret_day = None
    if not raw_date.strip():
        issues.append(_issue(ERROR, "C1", "返品日が入力されていません。"))
    else:
        d = parse_date(raw_date)
        if d is None:
            issues.append(_issue(ERROR, "C4", f"返品日「{raw_date}」を日付として読めません。"))
        else:
            ret_day = date(*map(int, d.split("/")))

    partner, p_issues = find_partner(con, code, company) if (code or company) else (None, [])
    issues += p_issues

    fiscal_info = None
    if partner is not None and ret_day is not None:
        fiscal_info = fiscal(ret_day, partner["closing_day"], confirm_day)
        if fiscal_info["late"]:
            issues.append(
                _issue(
                    WARNING,
                    "S8",
                    f"締め日（{fiscal_info['original_closing'].replace('-', '/')}）を過ぎたため、"
                    f"{int(fiscal_info['fiscal_month'][5:])}月計上にしました（翌月の請求で相殺）。",
                )
            )

    if partner is not None and slip_no:
        sql = "SELECT return_id FROM t_return_slip WHERE partner_code = ? AND slip_no = ?"
        args = [partner["partner_code"], slip_no]
        if exclude_return_id is not None:
            sql += " AND return_id <> ?"
            args.append(exclude_return_id)
        if con.execute(sql, args).fetchone():
            issues.append(_issue(WARNING, "C6", f"伝票番号 {slip_no} は、この取引先ですでに確定・出力されています。"))

    reasons = reason_index(con)
    rounding = partner["tax_rounding"] if partner is not None else "切り捨て"

    # ---- 明細 ----
    lines = []
    used = {}  # この伝票の中で、同じ売上明細を何個返品するか
    for page in sorted(judged_by_page):
        for row in judged_by_page[page]:
            if _is_empty_row(row):
                continue
            r = row["row"]
            ex = row.get("extra") or {}
            kind = ex.get("明細区分") or "通常"
            discount = kind == "値引"
            vals = {f: cell_value(row["cells"][f], f in NUMERIC) if f in row["cells"] else None for f in LINE_FIELDS}

            need = ["単価"] if discount else ["品番", "数量", "単価", "返品理由"]
            undecided = [f for f in need if vals.get(f) is None]
            if undecided:
                issues.append(
                    _issue(
                        ERROR,
                        "C1",
                        f"未確定の欄があります（{'・'.join(undecided)}）。OCRの自信が低い「要確認」の欄は、原本と見比べてから、正しければ採用、違えば正解を入力してください。",
                        page,
                        r,
                    )
                )
            for f in need:
                if vals.get(f) == "":
                    issues.append(_issue(ERROR, "C1", f"{f}が空欄です。", page, r))
            if not discount and not ex.get("良品不良"):
                issues.append(_issue(ERROR, "C1", "良品／不良が選ばれていません。", page, r))
            if discount and not ex.get("税率"):
                issues.append(_issue(ERROR, "C1", "値引の税率（10% / 8% / 按分）が選ばれていません。", page, r))

            # 数値（C2）
            qty = 1 if discount else None
            price = None
            if not discount and vals.get("数量"):
                n = parse_number(vals["数量"])
                if n is None or n != n.to_integral_value() or n < 1:
                    issues.append(_issue(ERROR, "C2", f"数量「{vals['数量']}」は1以上の整数にしてください。", page, r))
                else:
                    qty = int(n)
            if vals.get("単価"):
                n = parse_number(vals["単価"])
                if n is None:
                    issues.append(_issue(ERROR, "C2", f"単価「{vals['単価']}」が数値ではありません。", page, r))
                elif discount and n >= 0:
                    issues.append(_issue(ERROR, "C2", "値引の行は、単価（値引額）をマイナスにしてください。", page, r))
                elif not discount and n <= 0:
                    issues.append(_issue(ERROR, "C2", f"単価「{vals['単価']}」は0より大きくしてください。", page, r))
                else:
                    price = n

            # 返品理由（C3）
            reason_text = vals.get("返品理由") or ""
            reason = reasons.get(_norm(reason_text)) if reason_text else None
            if reason_text and reason is None:
                issues.append(
                    _issue(ERROR, "C3", f"返品理由「{reason_text}」が返品理由マスタにありません。選択肢から選んでください。", page, r)
                )

            line = {
                "page": page,
                "row": r,
                "明細区分": kind,
                "品番": "" if discount else _norm(vals.get("品番")),
                "枝番": "" if discount else _norm(vals.get("枝番")),
                "数量": qty,
                "単価": price,
                "金額": _round(qty * price, rounding) if (qty is not None and price is not None) else None,
                "理由コード": reason[0] if reason else "",
                "返品理由": reason[1] if reason else reason_text,
                "返品理由（伝票の文字）": reason_text,
                "良品不良": "" if discount else ex.get("良品不良", ""),
                "商品名": "値引" if discount else "",
                "税率の決め方": ex.get("税率") or "",
                "税率": int(ex["税率"]) if discount and ex.get("税率") in ("10", "8") else None,
                "候補": [],
                "元売上": None,
            }

            if not discount and line["品番"]:
                item = find_item(con, line["品番"], line["枝番"])
                if item is None:
                    issues.append(_issue(ERROR, "S4", f"品番「{line['品番']} {line['枝番']}」が商品マスタにありません。", page, r))
                else:
                    line["商品名"] = item["name"]
                    line["税率"] = item["tax_rate"]
                    line["税率の決め方"] = str(item["tax_rate"])

                if partner is not None:
                    cands = sales_candidates(
                        con, partner["partner_code"], line["品番"], line["枝番"],
                        _iso(ret_day) if ret_day else None, sales_no_hint or None, exclude_return_id,
                    )
                    if sales_no_hint and cands and cands[0]["partner_code"] != partner["partner_code"]:
                        issues.append(_issue(ERROR, "S3", f"元売上番号 {sales_no_hint} は、別の会社の売上です。", page, r))
                        cands = []
                    line["候補"] = [
                        {**c, "unit_price": str(c["unit_price"]), "remaining": c["remaining"] - used.get((c["sales_no"], c["line_no"]), 0)}
                        for c in cands
                    ]
                    chosen = None
                    ref = ex.get("元売上")
                    if ref:
                        chosen = next((c for c in line["候補"] if (c["sales_no"], c["line_no"]) == (ref["sales_no"], ref["line_no"])), None)
                        if chosen is None:
                            issues.append(_issue(ERROR, "S3", f"選んだ売上 {ref['sales_no']}（{ref['line_no']}行目）は、この明細の候補にありません。", page, r))
                    elif len(line["候補"]) == 1:
                        chosen = line["候補"][0]  # 1件だけなら照合先として表示する（確定するのは人）
                    elif len(line["候補"]) > 1:
                        issues.append(_issue(ERROR, "S3", f"元の売上の候補が {len(line['候補'])} 件あります。どの売上の返品か選んでください。", page, r))
                    if not line["候補"]:
                        where = f"売上番号 {sales_no_hint} に" if sales_no_hint else "返品日以前の売上に"
                        issues.append(_issue(ERROR, "S3", f"{where}、この品番・枝番の売上が見つかりません。", page, r))

                    if chosen is not None:
                        line["元売上"] = chosen
                        if qty is not None:
                            if qty > chosen["remaining"]:
                                issues.append(
                                    _issue(ERROR, "S5", f"返品数量 {qty} が、返品できる残り {chosen['remaining']} を超えています（売上 {chosen['sales_no']}）。", page, r)
                                )
                            used[(chosen["sales_no"], chosen["line_no"])] = used.get((chosen["sales_no"], chosen["line_no"]), 0) + qty
                        if price is not None and price != Decimal(chosen["unit_price"]):
                            issues.append(
                                _issue(WARNING, "S6", f"単価 {price} が、売ったときの単価 {chosen['unit_price']} と違います。", page, r)
                            )
                        if ret_day is not None and _iso(ret_day) < chosen["sales_date"]:
                            issues.append(_issue(WARNING, "S7", "返品日が、売上日より前になっています。", page, r))
            lines.append(line)

    if not lines:
        issues.append(_issue(ERROR, "C1", "明細が1行もありません。"))

    return {
        "header": {
            "会社コード": partner["partner_code"] if partner is not None else code,
            "会社名": partner["name"] if partner is not None else company,
            "会社名（伝票の文字）": company,
            "インボイス登録番号": partner["invoice_no"] if partner is not None else "",
            "端数処理": rounding,
            "伝票番号": slip_no,
            "返品日": ret_day.strftime("%Y/%m/%d") if ret_day else None,
            "元売上番号": sales_no_hint,
            **(fiscal_info or {"fiscal_month": None, "closing_date": None, "late": False}),
            # 1.2版の出力（import_format.json）が使う名前
            "取引先コード": partner["partner_code"] if partner is not None else code,
            "取引先名": partner["name"] if partner is not None else company,
        },
        "lines": lines,
        "tax": tax_summary(lines, rounding),
        "issues": issues,
        "errors": sum(i["level"] == ERROR for i in issues),
        "warnings": sum(i["level"] == WARNING for i in issues),
        "partner_found": partner is not None,
    }
