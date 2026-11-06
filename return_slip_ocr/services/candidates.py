"""OCRが読んだ文字を、マスタにある値と照らし合わせて直す（Issue #19 ⑤「マスタにある値を候補にする」）。

直すのは「マスタの値がちょうど1つだけ当てはまる」ときだけ。2つ以上当てはまる・1つも無いときは触らない（人が確認する）。

  コード（品番・元売上番号）… OCRが取り違えやすい文字（O と 0、I と 1、S と 5 など）を入れ替えてマスタと一致するか
                           → 「TEA-OO1」→「TEA-001」。それ以外の違い（FILE-0551 と FILE-0552）は別物なので直さない
  文字（返品理由）         … 1文字だけ違う理由名がマスタに1つだけあるか → 「サイス違い」→「サイズ違い」
"""
import itertools
import re
import unicodedata

# OCRが取り違えやすい文字（左右どちらにも読み間違える）
CONFUSABLE = [("O", "0"), ("I", "1"), ("L", "1"), ("S", "5"), ("$", "S"), ("B", "8"), ("Z", "2"), ("G", "6")]


def _norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).upper()


def _alternatives(ch):
    alts = {ch}
    for a, b in CONFUSABLE:
        if ch == a:
            alts.add(b)
        elif ch == b:
            alts.add(a)
    return sorted(alts)


def match_code(text, known, max_variants=4096):
    """text を取り違えやすい文字の入れ替えだけでマスタの値にできるなら、その値を返す。1つに決まらなければ None。"""
    t = _norm(text)
    if not t or not known:
        return None
    known_norm = {_norm(k): k for k in known}
    if t in known_norm:
        return known_norm[t]
    choices = [_alternatives(c) for c in t]
    n = 1
    for c in choices:
        n *= len(c)
    if n > max_variants:
        return None
    hits = {known_norm[v] for v in ("".join(p) for p in itertools.product(*choices)) if v in known_norm}
    return hits.pop() if len(hits) == 1 else None


def _distance(a, b):
    """1文字の置き換え・追加・削除を1と数えた距離（レーベンシュタイン距離）。"""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_text(text, known, max_distance=1, min_length=3):
    """text と1文字（max_distance）しか違わない値がマスタに1つだけあれば、その値を返す。"""
    t = _norm(text)
    if len(t) < min_length or not known:
        return None
    known_norm = {_norm(k): k for k in known}
    if t in known_norm:
        return known_norm[t]
    hits = {orig for k, orig in known_norm.items() if abs(len(k) - len(t)) <= max_distance and _distance(t, k) <= max_distance}
    return hits.pop() if len(hits) == 1 else None


def apply_known(items, rows, header_found, known):
    """明細の品番・返品理由と、見出しの元売上番号を、マスタの値で直す。直した枠には matched=True を付ける。

    known: {"品番": [...], "返品理由": [...], "元売上番号": [...]}（無い項目は飛ばす）
    """
    by_no = {it["no"]: it for it in items}

    def fix(nos, matcher, values):
        if len(nos) != 1 or nos[0] not in by_no:
            return
        it = by_no[nos[0]]
        hit = matcher(it["text"], values)
        if hit is not None and hit != it["text"]:
            it["text"], it["matched"] = hit, True

    for row in rows:
        if known.get("品番"):
            fix(row["cells"]["品番"]["ocr_nos"], match_code, known["品番"])
        if known.get("返品理由"):
            fix(row["cells"]["返品理由"]["ocr_nos"], match_text, known["返品理由"])
    f = (header_found or {}).get("元売上番号")
    if f and known.get("元売上番号"):
        fix(f["ocr_nos"], match_code, known["元売上番号"])
    return items


def known_from_db(con):
    """マスタ・売上データから、照らし合わせに使う値を取り出す。"""
    return {
        "品番": [r[0] for r in con.execute("SELECT DISTINCT item_no FROM m_item WHERE active = 1")],
        "返品理由": [r[0] for r in con.execute("SELECT name FROM m_return_reason WHERE active = 1")],
        "元売上番号": [r[0] for r in con.execute("SELECT sales_no FROM t_sales")],
    }
