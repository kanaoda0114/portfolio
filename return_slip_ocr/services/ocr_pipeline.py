"""1ページ分の読み取りの流れをひとまとめにする。

  画像 → OCR（文字枠）→ 明細表の行×項目に割り当て → （読み直しで精度を上げる）→ 見出しの抽出

アプリ（app.py）と、精度を測る道具（tools/ocr_benchmark.py）が同じ流れを使うので、
ここを直せば両方に効く。
"""
import config

from . import header_extract as hx
from .ocr_engine import run_ocr
from .candidates import apply_known
from .ocr_refine import refine_cells, refine_header
from .review import cell_ocr
from .table_lines import vertical_lines
from .table_mapper import propose_rows


def read_page(image, reader, refine=None, known=None):
    """OCRして明細の割り当てまで行う。

    known: マスタ・売上データの値 {"品番": [...], "返品理由": [...], "元売上番号": [...]}。
           渡すと、取り違えやすい文字の入れ替え・1文字違いで1つに決まる値に直す（services/candidates.py）
    戻り値: (items, proposal)
      items    … 文字枠のリスト（読み直した枠は text / confidence が置き換わる）
      proposal … table_mapper.propose_rows の結果
    """
    if refine is None:
        refine = config.REFINE_CELLS
    items = run_ocr(image, reader)
    v_lines = vertical_lines(image) if config.USE_TABLE_LINES else []
    proposal = propose_rows(
        items, config.TABLE_HEADERS, config.REVIEW_FIELDS, config.TABLE_END_LABELS, config.AUTO_ASSIGN_MIN_OVERLAP, v_lines
    )
    proposal["v_lines"] = v_lines
    if refine:
        refine_header(image, items, reader)
        if proposal["rows"]:
            refine_cells(image, items, proposal, reader)
    if known and config.USE_KNOWN_VALUES:
        apply_known(items, proposal["rows"], hx.extract_header(items), known)
    return items, proposal


def cell_texts(items, rows, fields=None):
    """行ごとに {項目: OCRの文字} を返す（表示・比較用）。"""
    fields = fields or config.REVIEW_FIELDS
    by_no = {it["no"]: it for it in items}
    return [{f: cell_ocr(by_no, row["cells"][f]["ocr_nos"])["text"] for f in fields} for row in rows]


def header_values(items):
    return hx.proposal_values(hx.extract_header(items))
