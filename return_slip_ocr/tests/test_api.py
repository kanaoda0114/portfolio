"""確定（F7）とExcel出力（F5）の流れを、画面と同じAPIで確かめるテスト。

OCRは実行せず、OCR結果を記録フォルダに直接置いてから操作する。
"""
import io
import os

import pymupdf
import pytest
from flask.testing import FlaskClient
from openpyxl import load_workbook

import app as app_module
import config
from services.doc_store import DocStore
from tests.helpers import GOOD_HEADER, make_page, normal_line


LOCAL_URL = f"http://127.0.0.1:{config.PORT}"


class LocalClient(FlaskClient):
    """このPCのブラウザから開いたときと同じアドレス（127.0.0.1）で送る。"""

    def open(self, *args, **kwargs):
        kwargs.setdefault("base_url", LOCAL_URL)
        return super().open(*args, **kwargs)


@pytest.fixture
def client(tmp_path, monkeypatch):
    records = tmp_path / "records"
    monkeypatch.setattr(app_module, "store", DocStore(str(records)))
    monkeypatch.setattr(config, "DEFAULT_EXPORT_DIR", str(records / "exports"))
    monkeypatch.setattr(config, "SETTINGS_PATH", str(records / "settings.json"))
    monkeypatch.setattr(config, "HISTORY_PATH", str(records / "export_history.csv"))
    monkeypatch.setattr(config, "DB_PATH", str(records / "returns.db"))
    monkeypatch.setattr(app_module.app, "test_client_class", LocalClient)
    return app_module.app.test_client()


def make_pdf(text="sample"):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


def upload(client, name="返品伝票.pdf", text="sample"):
    res = client.post("/api/docs", data={"pdf": (io.BytesIO(make_pdf(text)), name)}, content_type="multipart/form-data")
    assert res.status_code == 200, res.get_json()
    return res.get_json()["doc_id"]


def prepare(client, lines, header=GOOD_HEADER, name="返品伝票.pdf", text="sample"):
    """アップロード → OCR結果を置く → 明細を保存 → ヘッダを保存 まで行う。"""
    doc_id = upload(client, name, text)
    rows, items = make_page(lines)
    store = app_module.store
    doc = store.get(doc_id)
    store.save_ocr(doc, 1, {"page": 1, "dpi": 200, "width": 100, "height": 100, "items": items})
    res = client.put(f"/api/docs/{doc_id}/pages/1/review", json={"rows": rows})
    assert res.status_code == 200, res.get_json()
    res = client.put(f"/api/docs/{doc_id}/header", json=header)
    assert res.status_code == 200, res.get_json()
    return doc_id


# ---- 確定と出力の流れ（要件定義書 F5・F7） ----


def test_確定して出力すると出力済みになり履歴に残る(client):
    doc_id = prepare(client, [normal_line()])
    res = client.post(f"/api/docs/{doc_id}/confirm")
    assert res.status_code == 200
    assert res.get_json()["status"]["state"] == "確定済み"

    res = client.post("/api/export", json={"doc_ids": [doc_id]})
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["rows"] == 1
    assert os.path.exists(body["path"])
    ws = load_workbook(body["path"]).active
    assert ws.cell(row=2, column=2).value == "R-000123"

    # 20 出力履歴に追記され、状態が出力済みになる
    hist = open(config.HISTORY_PATH, encoding="utf-8-sig").read()
    assert "R-000123" in hist
    assert client.get(f"/api/docs/{doc_id}").get_json()["status"]["state"] == "出力済み"


def test_16_エラーがあると確定できない(client):
    doc_id = prepare(client, [normal_line(extra={"良品不良": ""})])
    res = client.post(f"/api/docs/{doc_id}/confirm")
    assert res.status_code == 400
    assert res.get_json()["code"] == "check_failed"
    assert client.get(f"/api/docs/{doc_id}").get_json()["status"]["state"] == "確認中"


def test_警告だけなら確定できる(client):
    doc_id = prepare(client, [normal_line(truth={"金額": "3700"})])
    res = client.post(f"/api/docs/{doc_id}/confirm")
    assert res.status_code == 200
    assert res.get_json()["check"]["warnings"] == 1


def test_確定後は編集できず取り消すと編集できる(client):
    doc_id = prepare(client, [normal_line()])
    client.post(f"/api/docs/{doc_id}/confirm")
    res = client.put(f"/api/docs/{doc_id}/header", json=GOOD_HEADER)
    assert res.status_code == 409

    res = client.delete(f"/api/docs/{doc_id}/confirm")
    assert res.status_code == 200
    res = client.put(f"/api/docs/{doc_id}/header", json=GOOD_HEADER)
    assert res.status_code == 200


def test_17_出力済みの伝票は確定を取り消せない(client):
    doc_id = prepare(client, [normal_line()])
    client.post(f"/api/docs/{doc_id}/confirm")
    client.post("/api/export", json={"doc_ids": [doc_id]})
    res = client.delete(f"/api/docs/{doc_id}/confirm")
    assert res.status_code == 409
    assert res.get_json()["code"] == "already_exported"


def test_19_出力できる伝票が0件ならファイルを作らない(client):
    doc_id = prepare(client, [normal_line()])  # 確定していない
    res = client.post("/api/export", json={"doc_ids": [doc_id]})
    assert res.status_code == 400
    assert res.get_json()["code"] == "nothing_to_export"
    d = config.DEFAULT_EXPORT_DIR
    assert not os.path.exists(d) or not [f for f in os.listdir(d) if f.endswith(".xlsx")]


def test_複数の伝票を1つのExcelにまとめ未確定の伝票は理由つきで除く(client):
    a = prepare(client, [normal_line()], name="a.pdf", text="a")
    b = prepare(client, [normal_line(), normal_line()], header={**GOOD_HEADER, "伝票番号": "R-000124"}, name="b.pdf", text="b")
    c = prepare(client, [normal_line()], header={**GOOD_HEADER, "伝票番号": "R-000125"}, name="c.pdf", text="c")
    client.post(f"/api/docs/{a}/confirm")
    client.post(f"/api/docs/{b}/confirm")
    body = client.post("/api/export", json={"doc_ids": [a, b, c]}).get_json()
    assert body["rows"] == 3
    assert body["skipped"] == [{"filename": "c.pdf", "reason": "確定していません"}]


def test_14_同じ伝票を2回出力すると重複の警告が出る(client):
    a = prepare(client, [normal_line()], name="a.pdf", text="a")
    client.post(f"/api/docs/{a}/confirm")
    client.post("/api/export", json={"doc_ids": [a]})
    b = prepare(client, [normal_line()], name="b.pdf", text="b")  # 同じ伝票番号
    check = client.get(f"/api/docs/{b}/check").get_json()["check"]
    assert [i["check"] for i in check["issues"]] == ["C6"]


def test_確認していないページがあると確定できない(client):
    doc_id = upload(client)
    client.put(f"/api/docs/{doc_id}/header", json=GOOD_HEADER)
    res = client.post(f"/api/docs/{doc_id}/confirm")
    assert res.status_code == 400
    assert res.get_json()["code"] == "pages_pending"


# ---- 出力先の設定（要件定義書 F5） ----


def test_出力先を設定するとそのフォルダに出力される(client, tmp_path):
    out = tmp_path / "共有フォルダ"
    out.mkdir()
    res = client.put("/api/settings", json={"export_dir": str(out)})
    assert res.status_code == 200, res.get_json()
    assert res.get_json()["export_dir"] == str(out)

    doc_id = prepare(client, [normal_line()])
    client.post(f"/api/docs/{doc_id}/confirm")
    body = client.post("/api/export", json={"doc_ids": [doc_id]}).get_json()
    assert os.path.dirname(body["path"]) == str(out)
    # 設定は記憶される
    assert client.get("/api/settings").get_json()["export_dir"] == str(out)


def _unused_drive():
    for letter in "QRSTUVWXY":
        if not os.path.exists(f"{letter}:\\"):
            return f"{letter}:\返品取込"
    pytest.skip("使われていないドライブ文字がありません")


def test_無いフォルダを指定すると作成してお知らせする(client, tmp_path):
    out = tmp_path / "新しいフォルダ" / "返品取込"
    res = client.put("/api/settings", json={"export_dir": str(out)})
    body = res.get_json()
    assert res.status_code == 200, body
    assert out.is_dir()
    assert body["notice"] == f"出力先フォルダが無かったので作成しました: {out}"


def test_あるフォルダを指定したときはお知らせしない(client, tmp_path):
    res = client.put("/api/settings", json={"export_dir": str(tmp_path)})
    assert res.get_json()["notice"] == ""


@pytest.mark.parametrize("kind", ["相対パス", "無いドライブ", "同じ名前のファイル"])
def test_使えない出力先は理由を表示して保存しない(client, tmp_path, kind):
    if kind == "相対パス":
        path = "返品取込"
    elif kind == "無いドライブ":
        path = _unused_drive()
    else:
        f = tmp_path / "返品取込"
        f.write_text("x")
        path = str(f)
    before = client.get("/api/settings").get_json()["export_dir"]
    res = client.put("/api/settings", json={"export_dir": path})
    body = res.get_json()
    assert res.status_code == 400
    assert body["code"] == "bad_export_dir"
    assert body["hint"]  # 対処方法も表示する
    assert client.get("/api/settings").get_json()["export_dir"] == before


def test_出力先を空にすると初期値に戻る(client, tmp_path):
    client.put("/api/settings", json={"export_dir": str(tmp_path)})
    res = client.put("/api/settings", json={"export_dir": ""})
    assert res.get_json()["export_dir_is_default"] is True


def test_設定したフォルダが消えていたら作り直して出力しお知らせする(client, tmp_path):
    out = tmp_path / "消えるフォルダ"
    out.mkdir()
    client.put("/api/settings", json={"export_dir": str(out)})
    out.rmdir()
    doc_id = prepare(client, [normal_line()])
    client.post(f"/api/docs/{doc_id}/confirm")
    body = client.post("/api/export", json={"doc_ids": [doc_id]}).get_json()
    assert body["ok"] is True
    assert "作成しました" in body["notice"]
    assert os.path.exists(body["path"])


def test_初めて出力するときは初期の出力先を作成する(client):
    doc_id = prepare(client, [normal_line()])
    client.post(f"/api/docs/{doc_id}/confirm")
    body = client.post("/api/export", json={"doc_ids": [doc_id]}).get_json()
    assert os.path.dirname(body["path"]) == config.DEFAULT_EXPORT_DIR
    assert "作成しました" in body["notice"]


# ---- セキュリティ ----


def test_ほかのサイトからの操作は断る(client):
    res = client.put("/api/settings", json={"export_dir": ""}, headers={"Origin": "https://evil.example.com"})
    assert res.status_code == 403
    assert res.get_json()["code"] == "bad_origin"


def test_このアプリの画面からの操作は通す(client):
    res = client.put("/api/settings", json={"export_dir": ""}, headers={"Origin": LOCAL_URL})
    assert res.status_code == 200


def test_127001以外のアドレスで来たリクエストは断る(client):
    res = client.get("/api/settings", base_url="http://evil.example.com")
    assert res.status_code == 403
    assert res.get_json()["code"] == "bad_host"


# ---- マスタ・売上データの取り込み（要件定義書 F8） ----


def test_見本Excelをダウンロードできる(client):
    res = client.get("/api/import/item/template")
    assert res.status_code == 200
    assert "attachment" in res.headers["Content-Disposition"]
    assert res.data[:2] == b"PK"  # xlsx（zip）の先頭


def test_画面からマスタを取り込めて件数が返る(client):
    data = "品番,枝番,商品名,単位,税率,有効\r\nNOTE-104,01,A5ノート,冊,10,\r\n".encode("utf-8")
    res = client.post("/api/import/item", data={"file": (io.BytesIO(data), "商品.csv")}, content_type="multipart/form-data")
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["imported"] is True
    assert body["counts"]["m_item"] == 1


def test_エラーがあると400で理由の一覧を返す(client):
    data = "品番,枝番,商品名,単位,税率,有効\r\nNOTE-104,01,A5ノート,冊,5,\r\n".encode("utf-8")
    res = client.post("/api/import/item", data={"file": (io.BytesIO(data), "商品.csv")}, content_type="multipart/form-data")
    body = res.get_json()
    assert res.status_code == 400
    assert body["code"] == "import_rejected"
    assert body["errors"][0] == {"row": 2, "column": "税率", "message": "10 または 8 にしてください"}
    assert client.get("/api/import/kinds").get_json()["counts"]["m_item"] == 0
