"""EasyOCR の呼び出しをまとめたモジュール。

Reader の生成（モデル読み込み）は重いので、最初の1回だけ行い使い回す。
OCR はすべてこのPC内で実行され、画像を外部に送ることはない。
"""
import threading

from .errors import AppError

_reader = None
_reader_info = {}
_init_lock = threading.Lock()
_ocr_lock = threading.Lock()  # EasyOCR はスレッドセーフではないので同時実行させない


def _decide_gpu(setting):
    if setting == "off":
        return False
    try:
        import torch

        available = torch.cuda.is_available()
    except Exception:
        available = False
    if setting == "on" and not available:
        raise AppError(
            "GPU使用（OCR_USE_GPU=on）が指定されていますが、CUDA対応GPUが使えません。",
            "OCR_USE_GPU を auto か off にしてください。",
            status=500,
            code="gpu_unavailable",
        )
    return available


def get_reader(langs, use_gpu="auto", model_dir=None, allow_download=True):
    global _reader
    with _init_lock:
        if _reader is not None:
            return _reader
        try:
            import easyocr
        except ImportError as e:
            raise AppError(
                f"EasyOCR を読み込めません: {e}",
                "アプリを起動している Python に easyocr が入っているか確認してください"
                "（py -m pip show easyocr）。",
                status=500,
                code="easyocr_missing",
            )

        gpu = _decide_gpu(use_gpu)
        try:
            _reader = easyocr.Reader(
                langs,
                gpu=gpu,
                model_storage_directory=model_dir,
                download_enabled=allow_download,
                verbose=False,
            )
        except Exception as e:
            hint = "モデルファイルが無い・壊れている可能性があります。"
            if not allow_download:
                hint += " config.py の ALLOW_MODEL_DOWNLOAD を True にして一度起動するか、モデルを手動で配置してください。"
            else:
                hint += " 初回はモデルのダウンロードにインターネット接続が必要です。"
            raise AppError(
                f"EasyOCR の初期化に失敗しました（{type(e).__name__}: {e}）",
                hint,
                status=500,
                code="easyocr_init_failed",
            )
        _reader_info.update({"langs": langs, "gpu": gpu, "version": getattr(easyocr, "__version__", "?")})
        return _reader


def reader_info():
    return dict(_reader_info)


def run_ocr(image, reader):
    """画像を OCR し、表示用の辞書のリストを返す。"""
    try:
        with _ocr_lock:
            raw = reader.readtext(image, detail=1, paragraph=False)
    except MemoryError:
        raise AppError(
            "OCR中にメモリが不足しました。",
            "config.py の RENDER_DPI を下げてください。",
            status=500,
            code="ocr_memory",
        )
    except Exception as e:
        raise AppError(f"OCR処理中にエラーが発生しました（{type(e).__name__}: {e}）", status=500, code="ocr_failed")

    items = []
    for i, (bbox, text, conf) in enumerate(raw, start=1):
        points = [[round(float(x), 1), round(float(y), 1)] for x, y in bbox]
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        items.append(
            {
                "no": i,
                "text": text,
                "confidence": round(float(conf), 4),
                "points": points,  # 左上→右上→右下→左下 の4点（画像ピクセル座標）
                "box": {
                    "x": min(xs),
                    "y": min(ys),
                    "w": round(max(xs) - min(xs), 1),
                    "h": round(max(ys) - min(ys), 1),
                },
            }
        )
    return items
