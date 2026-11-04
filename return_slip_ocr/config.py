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

# 正解を確認する明細の項目
REVIEW_FIELDS = ["品番", "枝番", "数量", "単価", "金額"]

# 比較時に桁区切りカンマ・通貨記号を表記差として扱う項目
NUMERIC_FIELDS = {"数量", "単価", "金額"}

# 明細表の見出し（列の位置を決めるのに使う）と、明細の終わりを示す文字
TABLE_HEADERS = ["No", "品番", "枝番", "品名", "数量", "単価", "金額"]
TABLE_END_LABELS = ["小計"]

# 文字枠の幅のうち、この割合以上が1つの列に入っているときだけ自動で割り当てる
AUTO_ASSIGN_MIN_OVERLAP = 0.8
