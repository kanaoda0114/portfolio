"""マスタ・売上データを Excel / CSV からデータベースに取り込む（要件定義書 F8、データ設計書 4章）。

流れ: ファイルを読む → 全行をチェック → 1件でもエラーがあれば何も入れない → 問題なければまとめて入れる
- 取引先・商品・返品理由マスタ: キーが同じ行は上書き（最新のマスタに合わせる）
- 売上データ: 売上番号が同じものは入れ替え（売上の訂正に対応）。ただし返品済みの数より減らす入れ替えはしない
"""
import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .errors import AppError
from .slip_check import parse_date

MAX_ROWS = 50_000
MAX_ERRORS_SHOWN = 200


@dataclass
class Col:
    name: str  # ファイルの列名（日本語）
    key: str  # データベースの列名
    kind: str = "text"  # text / code / int / num / date / bool / choice
    required: bool = False
    choices: tuple = ()
    max_len: int = 100
    note: str = ""  # 見本Excelの2行目に出す説明


@dataclass
class Dataset:
    kind: str
    label: str
    cols: list
    keys: tuple  # 1ファイルの中で重複してはいけない列（key 名）
    file_label: str = ""
    extra_notes: list = field(default_factory=list)


ROUNDING = ("切り捨て", "四捨五入", "切り上げ")

DATASETS = {
    "partner": Dataset(
        "partner",
        "取引先マスタ",
        [
            Col("会社コード", "partner_code", "code", True, note="数字6桁（例: 100001）"),
            Col("会社名", "name", required=True, note="正式名称"),
            Col("会社名カナ", "name_kana"),
            Col("別名", "aliases", max_len=300, note="略称など。; 区切り"),
            Col("インボイス登録番号", "invoice_no", note="T＋13桁"),
            Col("締日", "closing_day", "int", True, note="1〜28、末日は 99 または「末」"),
            Col("支払サイト", "payment_site", required=True, note="例: 翌月末"),
            Col("消費税の端数処理", "tax_rounding", "choice", True, ROUNDING, note="切り捨て / 四捨五入 / 切り上げ"),
            Col("有効", "active", "bool", note="空・1・有効 → 有効、0・無効 → 無効"),
        ],
        ("partner_code",),
    ),
    "item": Dataset(
        "item",
        "商品マスタ",
        [
            Col("品番", "item_no", "code", True),
            Col("枝番", "branch_no", "code", note="無い商品は空"),
            Col("商品名", "name", required=True),
            Col("単位", "unit", max_len=10),
            Col("税率", "tax_rate", "int", True, note="10 または 8"),
            Col("有効", "active", "bool"),
        ],
        ("item_no", "branch_no"),
    ),
    "reason": Dataset(
        "reason",
        "返品理由マスタ",
        [
            Col("理由コード", "reason_code", "code", True),
            Col("理由名", "name", required=True),
            Col("別の書き方", "aliases", max_len=300, note="伝票に書かれる言い方。; 区切り"),
            Col("表示順", "sort_order", "int"),
            Col("有効", "active", "bool"),
        ],
        ("reason_code",),
    ),
    "sales": Dataset(
        "sales",
        "売上データ",
        [
            Col("売上番号", "sales_no", "code", True, note="納品書番号"),
            Col("会社コード", "partner_code", "code", True),
            Col("売上日", "sales_date", "date", True, note="例: 2026/10/01"),
            Col("行番号", "line_no", "int", True),
            Col("品番", "item_no", "code", True),
            Col("枝番", "branch_no", "code"),
            Col("数量", "qty", "int", True),
            Col("単価", "unit_price", "num", True, note="税込"),
            Col("金額", "amount", "int", note="税込。空なら 数量×単価"),
        ],
        ("sales_no", "line_no"),
        extra_notes=["1行＝売上明細1行。同じ売上番号の行は、会社コードと売上日をそろえてください。"],
    ),
}


# ---------------------------------------------------------------------------
# ファイルを読む
# ---------------------------------------------------------------------------


def read_table(filename, data):
    """Excel / CSV を (見出し, [(行番号, 値のリスト), ...]) にする。行番号はファイル上の行。"""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as e:
            raise AppError(f"Excelファイルを開けません: {filename}（{e}）", "Excelで開けるか確認し、.xlsx で保存し直してください。", code="import_unreadable")
        ws = wb.worksheets[0]
        rows = [(i, list(r)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1)]
    elif name.endswith(".csv"):
        text = None
        for enc in ("utf-8-sig", "cp932"):  # 販売管理システムの CSV は Shift_JIS が多い
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise AppError(f"CSVの文字コードを判定できません: {filename}", "UTF-8 または Shift_JIS で保存してください。", code="import_encoding")
        rows = [(i, r) for i, r in enumerate(csv.reader(io.StringIO(text)), start=1)]
    else:
        raise AppError(f"取り込めるのは Excel（.xlsx）と CSV（.csv）です: {filename}", code="import_bad_type")

    rows = [(i, r) for i, r in rows if any(v not in (None, "") and str(v).strip() for v in r)]
    if not rows:
        raise AppError(f"ファイルにデータがありません: {filename}", code="import_empty")
    header_line, header = rows[0]
    if len(rows) - 1 > MAX_ROWS:
        raise AppError(f"行が多すぎます（{len(rows) - 1}行）。1回に取り込めるのは {MAX_ROWS} 行までです。", code="import_too_many")
    return [str(h).strip() if h is not None else "" for h in header], rows[1:]


# ---------------------------------------------------------------------------
# 値のチェックと変換
# ---------------------------------------------------------------------------


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # Excel で数値として入った 100001.0 など
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y/%m/%d")
    return unicodedata.normalize("NFKC", str(v)).strip()


def convert(col, raw):
    """1つの値を変換する。戻り値: (値, エラー文 or None)。空なら (None, None)。"""
    if isinstance(raw, (datetime, date)) and col.kind == "date":
        return raw.strftime("%Y-%m-%d"), None
    s = _text(raw)
    if s == "":
        return None, None
    if col.kind in ("text", "code"):
        if len(s) > col.max_len:
            return None, f"{col.max_len}文字以内にしてください"
        if col.kind == "code" and not re.fullmatch(r"[0-9A-Za-z\-_/.]+", s):
            return None, f"「{s}」に使えない文字があります（英数字と - _ / . のみ）"
        if s.startswith(("=", "+", "@")):
            return None, "「=」「+」「@」で始まる値は使えません"
        return s, None
    if col.kind == "int":
        if col.key == "closing_day" and s in ("末", "末日"):
            return 99, None
        s2 = s.rstrip("%")
        if not re.fullmatch(r"-?\d+(\.0+)?", s2.replace(",", "")):
            return None, f"「{s}」は整数ではありません"
        return int(Decimal(s2.replace(",", ""))), None
    if col.kind == "num":
        try:
            return Decimal(s.replace(",", "").lstrip("¥\\").rstrip("円")), None
        except InvalidOperation:
            return None, f"「{s}」は数値ではありません"
    if col.kind == "date":
        d = parse_date(s)
        return (d.replace("/", "-"), None) if d else (None, f"「{s}」を日付として読めません")
    if col.kind == "bool":
        if s.lower() in ("1", "有効", "true", "○", "yes"):
            return 1, None
        if s.lower() in ("0", "無効", "false", "×", "no"):
            return 0, None
        return None, f"「{s}」は 1（有効）か 0（無効）で入れてください"
    if col.kind == "choice":
        return (s, None) if s in col.choices else (None, f"「{s}」は {' / '.join(col.choices)} のどれかにしてください")
    raise ValueError(col.kind)


def _err(errors, line, col, msg):
    errors.append({"row": line, "column": col, "message": msg})


def _row_checks(kind, rec, line, errors):
    """1行の中での、列どうしの関係のチェック。"""
    if kind == "partner":
        cd = rec.get("closing_day")
        if cd is not None and not (1 <= cd <= 28 or cd == 99):
            _err(errors, line, "締日", "1〜28、または末日（99）にしてください")
        inv = rec.get("invoice_no")
        if inv and not re.fullmatch(r"T\d{13}", inv):
            _err(errors, line, "インボイス登録番号", "「T」と13桁の数字にしてください")
        code = rec.get("partner_code")
        if code and not re.fullmatch(r"\d{6}", code):
            _err(errors, line, "会社コード", "数字6桁にしてください")
    elif kind == "item":
        if rec.get("tax_rate") not in (None, 8, 10):
            _err(errors, line, "税率", "10 または 8 にしてください")
    elif kind == "sales":
        qty, price, amount = rec.get("qty"), rec.get("unit_price"), rec.get("amount")
        if qty is not None and qty <= 0:
            _err(errors, line, "数量", "1以上にしてください")
        if price is not None and price < 0:
            _err(errors, line, "単価", "0以上にしてください")
        if qty is not None and price is not None:
            calc = qty * price
            if amount is None:
                if calc != calc.to_integral_value():
                    _err(errors, line, "金額", "数量×単価が整数にならないため、金額を入れてください")
                else:
                    rec["amount"] = int(calc)
            elif abs(Decimal(amount) - calc) > 1:
                _err(errors, line, "金額", f"数量×単価（{calc}）と金額（{amount}）が合いません")


def validate(kind, header, rows):
    """全行をチェックし、(取り込む行のリスト, エラーのリスト, お知らせのリスト) を返す。"""
    ds = DATASETS[kind]
    errors, notices = [], []
    pos = {}
    for i, h in enumerate(header):
        if h in pos:
            errors.append({"row": 1, "column": h, "message": "同じ列名が2つあります"})
        pos[h] = i
    missing = [c.name for c in ds.cols if c.required and c.name not in pos]
    if missing:
        errors.append({"row": 1, "column": "、".join(missing), "message": "必須の列がありません（見本Excelの列名に合わせてください）"})
        return [], errors, notices
    unknown = [h for h in header if h and h not in {c.name for c in ds.cols}]
    if unknown:
        notices.append(f"使わない列を無視しました: {'、'.join(unknown)}")

    records, seen = [], {}
    for line, values in rows:
        rec = {}
        for c in ds.cols:
            raw = values[pos[c.name]] if c.name in pos and pos[c.name] < len(values) else None
            v, msg = convert(c, raw)
            if msg:
                _err(errors, line, c.name, msg)
            elif v is None and c.required:
                _err(errors, line, c.name, "必須です")
            rec[c.key] = v
        # 省略できる列の初期値
        if "active" in rec and rec["active"] is None:
            rec["active"] = 1
        if "branch_no" in rec and rec["branch_no"] is None:
            rec["branch_no"] = ""
        if kind == "reason" and rec.get("sort_order") is None:
            rec["sort_order"] = len(records) + 1
        _row_checks(kind, rec, line, errors)

        key = tuple(rec.get(k) for k in ds.keys)
        if all(k is not None for k in key):
            if key in seen:
                names = "・".join(c.name for c in ds.cols if c.key in ds.keys)
                _err(errors, line, names, f"{seen[key]}行目と同じ {names} です")
            else:
                seen[key] = line
        rec["_line"] = line
        records.append(rec)
    return records, errors, notices


# ---------------------------------------------------------------------------
# データベースに入れる
# ---------------------------------------------------------------------------


def _ref_checks(con, kind, records, errors):
    """ほかのテーブルを見るチェック（会社コード・品番があるか、返品済みの数を下回らないか）。"""
    if kind != "sales":
        return
    partners = {r[0] for r in con.execute("SELECT partner_code FROM m_partner")}
    items = {(r[0], r[1]) for r in con.execute("SELECT item_no, branch_no FROM m_item")}
    heads = {}
    for rec in records:
        line = rec["_line"]
        if rec["partner_code"] and rec["partner_code"] not in partners:
            _err(errors, line, "会社コード", f"「{rec['partner_code']}」が取引先マスタにありません（先に取引先マスタを取り込んでください）")
        if rec["item_no"] and (rec["item_no"], rec["branch_no"]) not in items:
            _err(errors, line, "品番・枝番", f"「{rec['item_no']} {rec['branch_no']}」が商品マスタにありません")
        no = rec["sales_no"]
        if no:
            h = (rec["partner_code"], rec["sales_date"])
            if no in heads and heads[no][0] != h:
                _err(errors, line, "会社コード・売上日", f"同じ売上番号 {no} の {heads[no][1]}行目と、会社コードか売上日が違います")
            heads.setdefault(no, (h, line))

    # 入れ替えで、返品済みの明細が消えたり、返品済みの数より少なくなったりしないか
    new_lines = {(r["sales_no"], r["line_no"]): r for r in records}
    for no in heads:
        for row in con.execute(
            "SELECT sales_line_no, SUM(qty) FROM t_return_line WHERE sales_no = ? GROUP BY sales_line_no", (no,)
        ):
            line_no, returned = row[0], row[1]
            new = new_lines.get((no, line_no))
            if new is None:
                _err(errors, heads[no][1], "売上番号", f"売上 {no} の {line_no}行目はすでに返品されているため、なくせません")
            elif new["qty"] is not None and new["qty"] < returned:
                _err(errors, new["_line"], "数量", f"売上 {no} の {line_no}行目はすでに {returned} 個返品されているため、それより少なくできません")


def _save(con, kind, records, now):
    if kind == "partner":
        con.executemany(
            """INSERT INTO m_partner (partner_code, name, name_kana, aliases, invoice_no, closing_day, payment_site, tax_rounding, active, updated_at)
               VALUES (:partner_code, :name, :name_kana, :aliases, :invoice_no, :closing_day, :payment_site, :tax_rounding, :active, :now)
               ON CONFLICT (partner_code) DO UPDATE SET name=excluded.name, name_kana=excluded.name_kana, aliases=excluded.aliases,
                 invoice_no=excluded.invoice_no, closing_day=excluded.closing_day, payment_site=excluded.payment_site,
                 tax_rounding=excluded.tax_rounding, active=excluded.active, updated_at=excluded.updated_at""",
            [{**r, "now": now} for r in records],
        )
        return {"inserted_or_updated": len(records)}
    if kind == "item":
        con.executemany(
            """INSERT INTO m_item (item_no, branch_no, name, unit, tax_rate, active, updated_at)
               VALUES (:item_no, :branch_no, :name, :unit, :tax_rate, :active, :now)
               ON CONFLICT (item_no, branch_no) DO UPDATE SET name=excluded.name, unit=excluded.unit,
                 tax_rate=excluded.tax_rate, active=excluded.active, updated_at=excluded.updated_at""",
            [{**r, "now": now} for r in records],
        )
        return {"inserted_or_updated": len(records)}
    if kind == "reason":
        con.executemany(
            """INSERT INTO m_return_reason (reason_code, name, aliases, sort_order, active)
               VALUES (:reason_code, :name, :aliases, :sort_order, :active)
               ON CONFLICT (reason_code) DO UPDATE SET name=excluded.name, aliases=excluded.aliases,
                 sort_order=excluded.sort_order, active=excluded.active""",
            records,
        )
        return {"inserted_or_updated": len(records)}
    # 売上: 売上番号ごとに入れ替える
    nos = list(dict.fromkeys(r["sales_no"] for r in records))
    replaced = sum(
        con.execute("SELECT COUNT(*) FROM t_sales WHERE sales_no = ?", (n,)).fetchone()[0] for n in nos
    )
    con.execute("PRAGMA defer_foreign_keys = ON")  # 返品明細が参照している行を、入れ直す間だけ待ってもらう
    for n in nos:
        con.execute("DELETE FROM t_sales_line WHERE sales_no = ?", (n,))
        con.execute("DELETE FROM t_sales WHERE sales_no = ?", (n,))
    seen = set()
    for r in records:
        if r["sales_no"] not in seen:
            con.execute(
                "INSERT INTO t_sales (sales_no, partner_code, sales_date) VALUES (?, ?, ?)",
                (r["sales_no"], r["partner_code"], r["sales_date"]),
            )
            seen.add(r["sales_no"])
        con.execute(
            "INSERT INTO t_sales_line (sales_no, line_no, item_no, branch_no, qty, unit_price, amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["sales_no"], r["line_no"], r["item_no"], r["branch_no"], r["qty"], str(r["unit_price"]), r["amount"]),
        )
    return {"sales": len(nos), "lines": len(records), "replaced_sales": replaced}


def import_file(con, kind, filename, data, now):
    """取り込む。エラーがあれば何も入れずに結果を返す。"""
    if kind not in DATASETS:
        raise AppError(f"取り込みの種類が正しくありません: {kind}", code="bad_request")
    header, rows = read_table(filename, data)
    records, errors, notices = validate(kind, header, rows)
    if not errors:
        _ref_checks(con, kind, records, errors)
    result = {
        "kind": kind,
        "label": DATASETS[kind].label,
        "filename": filename,
        "rows": len(records),
        "errors": errors[:MAX_ERRORS_SHOWN],
        "error_count": len(errors),
        "notices": notices,
        "imported": False,
    }
    if errors:
        return result
    try:
        with con:  # 全部成功するか、何も変えないか（トランザクション）
            result["saved"] = _save(con, kind, records, now)
    except Exception as e:
        raise AppError(f"取り込み中にエラーが起きたため、何も変更していません（{e}）", status=500, code="import_failed")
    result["imported"] = True
    return result


# ---------------------------------------------------------------------------
# 見本Excel
# ---------------------------------------------------------------------------


def template_xlsx(kind):
    """列名と説明だけが入った、取り込み用の見本Excel（bytes）。"""
    ds = DATASETS[kind]
    wb = Workbook()
    ws = wb.active
    ws.title = ds.label
    thin = Side(style="thin", color="999999")
    for ci, c in enumerate(ds.cols, start=1):
        h = ws.cell(row=1, column=ci, value=c.name)
        h.font = Font(bold=True, color="FFFFFF")
        h.fill = PatternFill("solid", fgColor="1F3A8A" if c.required else "5B6B8C")
        h.alignment = Alignment(horizontal="center")
        h.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        letter = h.column_letter
        ws.column_dimensions[letter].width = max(12, len(c.name) * 2 + 4)
        # 文字の列は「文字列」書式にしておく（01 や 100001 が数値に化けないように）
        if c.kind in ("text", "code", "choice"):
            for r in range(2, 1002):
                ws.cell(row=r, column=ci).number_format = "@"
    ws.freeze_panes = "A2"

    info = wb.create_sheet("説明")
    info.append(["列名", "必須", "説明"])
    for c in ds.cols:
        info.append([c.name, "○" if c.required else "", c.note])
    for n in ds.extra_notes:
        info.append(["", "", n])
    info.append(["", "", "1行目の列名は変えないでください。濃い青の列は必須です。"])
    info.column_dimensions["A"].width = 18
    info.column_dimensions["C"].width = 60
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
