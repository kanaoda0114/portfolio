"""アプリ全体の設定値。値を変えたいときはこのファイルだけ編集すればよい。"""
import os

# ローカルサーバー（127.0.0.1 なので同じPCのブラウザからしかアクセスできない）
HOST = "127.0.0.1"
PORT = int(os.environ.get("OCR_APP_PORT", "5000"))

# アップロード上限（MB）
MAX_UPLOAD_MB = 20

# PDF → 画像化するときの解像度。上げると小さい文字に強くなるが遅くなる。
RENDER_DPI = 200

# 画像化したときの最大ピクセル数（巨大ページでメモリ不足になるのを防ぐ）
MAX_RENDER_PIXELS = 40_000_000

# EasyOCR の言語（日本語 + 英数字）
OCR_LANGS = ["ja", "en"]

# GPU を使うか: "auto"（CUDAが使えれば使う） / "on" / "off"
USE_GPU = os.environ.get("OCR_USE_GPU", "auto")

# EasyOCR のモデル保存先。未指定なら EasyOCR 既定（%USERPROFILE%\.EasyOCR\model）
MODEL_DIR = os.environ.get("EASYOCR_MODEL_DIR") or None

# モデルが未ダウンロードのとき、自動ダウンロードを許可するか。
# ダウンロードされるのはモデルファイルだけで、PDFや画像は一切送信しない。
# 完全オフラインで使うなら False にする（モデルが無いとエラーで知らせる）。
ALLOW_MODEL_DOWNLOAD = True

# 画面で「低信頼度」として色分けするしきい値
LOW_CONFIDENCE = 0.5

# ---- 読み取り精度の記録 ----

# 記録（OCR結果のキャッシュ・確認結果・CSV）を保存するフォルダ
RECORDS_DIR = os.environ.get("OCR_RECORDS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "records")

# OCRで読んで、正解を確認する明細の項目（要件定義書 1.4版 F3）
# 金額は読まない（数量×単価で計算する）
REVIEW_FIELDS = ["品番", "枝番", "数量", "単価", "返品理由"]

# 比較時に桁区切りカンマ・通貨記号を表記差として扱う項目
NUMERIC_FIELDS = {"数量", "単価"}

# 明細表の見出し（列の位置を決めるのに使う）と、明細の終わりを示す文字（この文字で始まる行で終わり）
TABLE_HEADERS = ["No", "品番", "枝番", "品名", "数量", "単価", "金額", "返品理由"]
TABLE_END_LABELS = ["小計", "合計", "総合計"]

# 文字枠の幅のうち、この割合以上が1つの列に入っているときだけ自動で割り当てる
AUTO_ASSIGN_MIN_OVERLAP = 0.8

# 明細表の縦の罫線を画像から見つけて、列の境界に使う（services/table_lines.py、Issue #19）
USE_TABLE_LINES = True

# ---- 欄の読み直し（services/ocr_refine.py、Issue #19） ----

# 明細と見出しの欄を1つずつ切り出して読み直す。False なら最初のOCR結果のまま
REFINE_CELLS = True

# 列ごとに「出てくる文字」。ここに無い項目（返品理由）は読み直さない
_DIGITS = "0123456789"
_CODE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + _DIGITS + "-"
REFINE_ALLOWLIST = {
    "品番": _CODE,
    "枝番": _DIGITS,
    "数量": _DIGITS,
    "単価": _DIGITS + ",-",
}
REFINE_HEADER_ALLOWLIST = {
    "会社コード": _DIGITS,
    "伝票番号": _CODE,
    "返品日": _DIGITS + "/",
    "元売上番号": _CODE,
}

# 切り出した欄の画像を、この高さ（px）になるまで拡大してから読む
REFINE_TARGET_HEIGHT = 64

# 最初のOCRで何も見つからなかった欄を読み直したとき、この信頼度以上なら採用する
REFINE_MIN_CONF_NEW = 0.6

# 欄の中で黒い部分がこの割合未満なら空欄とみなし、読み直さない
REFINE_MIN_INK = 0.004

# 読んだ文字をマスタ・売上データの値で直す（services/candidates.py）。
# 取り違えやすい文字（O/0 など）の入れ替えや1文字違いで、マスタの値が1つだけ当てはまるときだけ直す
USE_KNOWN_VALUES = True

# ---- 確定と取込用Excelの出力（docs/要件定義書.md F4〜F7） ----

_HERE = os.path.dirname(os.path.abspath(__file__))

# 取込用Excelの出力先。利用者が画面で設定した値（SETTINGS_PATH に保存）が優先され、
# 未設定のときはこのフォルダを使う
DEFAULT_EXPORT_DIR = os.path.join(RECORDS_DIR, "exports")

# 画面で変えた設定（出力先など）の保存先
SETTINGS_PATH = os.path.join(RECORDS_DIR, "settings.json")

# 出力履歴（重複チェック C6 に使う）。出力先を変えても1か所にまとめる
HISTORY_PATH = os.path.join(RECORDS_DIR, "export_history.csv")

# Excelの列の並び・列名・コード値
IMPORT_FORMAT_PATH = os.path.join(_HERE, "import_format.json")

# ※1.2版の「取引先マスタ.csv」と「金額の許容差（C3）」は、1.4版でデータベースのマスタと
#   数量×単価の計算に置き換えたため、なくした（docs/要件定義書.md 1.4版）

# マスタ・売上データ・返品記録のデータベース（docs/データ設計書.md）
DB_PATH = os.path.join(RECORDS_DIR, "returns.db")

# マスタ・売上データの取り込みファイルの上限（MB）
MAX_IMPORT_MB = 20
