"""OCR結果から、伝票の見出し項目（会社名・会社コード・伝票番号・返品日・元売上番号）を探す（要件定義書 F3）。

探し方は「見出しの文字を見つけて、その右にある文字を値とする」だけ。
値の中身から推測して埋めることはしない（見つからなければ空のまま、人が入力する）。
"""
import re
import unicodedata

HEADER_FIELDS = ("会社名", "会社コード", "伝票番号", "返品日", "元売上番号")

# 見出しの言い方（長いものから順に照らし合わせる）
LABELS = {
    "会社コード": ["取引先コード", "得意先コード", "会社コード", "取引先CD", "得意先CD"],
    "伝票番号": ["返品伝票番号", "伝票番号", "伝票No", "伝票NO", "伝票№"],
    "返品日": ["返品年月日", "返品日", "伝票日付", "日付"],
    "元売上番号": ["元伝票番号", "元伝票No", "元売上番号", "納品書番号", "納品書No", "納品書NO"],
}
COMPANY_SUFFIX = "御中"
COMPANY_LABELS = ["取引先名", "得意先名", "取引先", "得意先"]


def _norm(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def _cy(it):
    return it["box"]["y"] + it["box"]["h"] / 2


def _same_row(a, b):
    h = max(a["box"]["h"], b["box"]["h"], 1)
    return abs(_cy(a) - _cy(b)) <= h * 0.6


def _right_neighbor(label_item, items, exclude):
    """見出しと同じ行で、右にあるいちばん近い文字枠。"""
    right_edge = label_item["box"]["x"] + label_item["box"]["w"]
    cands = [
        it
        for it in items
        if it["no"] not in exclude and _same_row(label_item, it) and it["box"]["x"] >= right_edge - 5
    ]
    return min(cands, key=lambda it: it["box"]["x"]) if cands else None


def _left_neighbor(item, items, exclude):
    left_edge = item["box"]["x"]
    cands = [
        it
        for it in items
        if it["no"] not in exclude and _same_row(item, it) and it["box"]["x"] + it["box"]["w"] <= left_edge + 5
    ]
    return max(cands, key=lambda it: it["box"]["x"]) if cands else None


def _found(value, items_used):
    return {
        "value": value,
        "ocr_nos": [it["no"] for it in items_used],
        "confidence": min(it["confidence"] for it in items_used) if items_used else None,
    }


def _all_labels():
    out = [lab for labs in LABELS.values() for lab in labs] + COMPANY_LABELS + [COMPANY_SUFFIX]
    return sorted(out, key=len, reverse=True)


def extract_header(items):
    """戻り値: {項目: {"value": 文字, "ocr_nos": [...], "confidence": 数値} or None}"""
    items = items or []
    all_labels = _all_labels()
    # 見出しそのものの文字枠（値の候補から外す）
    label_nos = {it["no"] for it in items if _norm(it["text"]) in all_labels}
    result = {f: None for f in HEADER_FIELDS}

    for field, labels in LABELS.items():
        best = None
        for it in items:
            t = _norm(it["text"])
            lab = next((lb for lb in sorted(labels, key=len, reverse=True) if t.startswith(lb)), None)
            if not lab:
                continue
            # 「伝票番号 R-000123」のように、見出しと値が1つの枠に入っていることがある
            rest = t[len(lab):].lstrip(":：")
            if rest:
                cand = _found(rest, [it])
            else:
                nb = _right_neighbor(it, items, label_nos | {it["no"]})
                cand = _found(_norm(nb["text"]), [nb]) if nb else None
            # 見出しの言い方が長いほど確か（「日付」より「返品日」を優先）
            if cand and (best is None or len(lab) > best[0]):
                best = (len(lab), cand)
        result[field] = best[1] if best else None

    # 会社名：「〇〇 御中」、なければ「取引先」の右
    for it in items:
        t = unicodedata.normalize("NFKC", it["text"]).strip()
        if t.endswith(COMPANY_SUFFIX):
            name = t[: -len(COMPANY_SUFFIX)].strip()
            if name:
                result["会社名"] = _found(name, [it])
            else:
                nb = _left_neighbor(it, items, label_nos | {it["no"]})
                if nb:
                    result["会社名"] = _found(unicodedata.normalize("NFKC", nb["text"]).strip(), [nb, it])
            break
    if result["会社名"] is None:
        for it in items:
            if _norm(it["text"]) in COMPANY_LABELS:
                nb = _right_neighbor(it, items, label_nos | {it["no"]})
                if nb:
                    result["会社名"] = _found(unicodedata.normalize("NFKC", nb["text"]).strip(), [nb])
                    break
    return result


def proposal_values(extracted):
    """画面の入力欄に入れる候補の値だけを取り出す。"""
    return {f: (v["value"] if v else "") for f, v in extracted.items()}
