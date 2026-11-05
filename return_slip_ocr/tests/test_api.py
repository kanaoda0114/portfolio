"""確定（F7）とExcel出力（F5）の流れを、画面と同じAPIで確かめるテスト。

OCRは実行せず、OCR結果を記録フォルダに直接置いてから操作する。
"""
import io
import os
from datetime import date

import pymupdf
import pytest
from flask.testing import FlaskClient
from openpyxl import load_workbook

import app as app_module
import config
from services import db as dbm
from services.doc_store import DocStore
from tests.helpers import GOOD_HEADER, load_samples, make_page, normal_line


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
    monkeypatch.setattr(app_module, "_today", lambda: date(2026, 11, 5))
    con = dbm.connect(config.DB_PATH)  # 架空のマスタと売上データを入れておく
    load_samples(con)
    con.close()
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
    doc_id = prepare(client, [normal_line(truth={"単価": "230"})])  # 売ったときは240円
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
    before = client.get("/api/import/kinds").get_json()["counts"]["m_item"]
    data = "品番,枝番,商品名,単位,税率,有効\r\nNEW-001,01,新しい商品,冊,10,\r\n".encode("utf-8")
    res = client.post("/api/import/item", data={"file": (io.BytesIO(data), "商品.csv")}, content_type="multipart/form-data")
    body = res.get_json()
    assert res.status_code == 200, body
    assert body["imported"] is True
    assert body["counts"]["m_item"] == before + 1


def test_エラーがあると400で理由の一覧を返す(client):
    before = client.get("/api/import/kinds").get_json()["counts"]["m_item"]
    data = "品番,枝番,商品名,単位,税率,有効\r\nNOTE-104,01,A5ノート,冊,5,\r\n".encode("utf-8")
    res = client.post("/api/import/item", data={"file": (io.BytesIO(data), "商品.csv")}, content_type="multipart/form-data")
    body = res.get_json()
    assert res.status_code == 400
    assert body["code"] == "import_rejected"
    assert body["errors"][0] == {"row": 2, "column": "税率", "message": "10 または 8 にしてください"}
    assert client.get("/api/import/kinds").get_json()["counts"]["m_item"] == before


# ---- 1.4版：見出しの候補・返品の記録・照合（要件定義書 F3・F7・F9） ----


def test_1ページ目のOCR結果から見出しの候補を返す(client):
    import json

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample_slip_ocr.json")
    ocr = json.load(open(fixture, encoding="utf-8"))
    doc_id = upload(client)
    app_module.store.save_ocr(app_module.store.get(doc_id), 1, {"page": 1, "dpi": 200, "width": 1, "height": 1, "items": ocr["items"]})
    prop = client.get(f"/api/docs/{doc_id}/header").get_json()["proposal"]
    assert {k: v["value"] for k, v in prop.items()} == {
        "会社名": "株式会社サンプル商事",
        "会社コード": "100001",
        "伝票番号": "R-000123",
        "返品日": "2026/11/02",
        "元売上番号": "S-24001",
    }


def _db_count(table):
    con = dbm.connect(config.DB_PATH)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def test_確定すると返品の記録に入り取り消すと外れる(client):
    doc_id = prepare(client, [normal_line()])
    client.post(f"/api/docs/{doc_id}/confirm")
    assert (_db_count("t_return_slip"), _db_count("t_return_line")) == (1, 1)
    client.delete(f"/api/docs/{doc_id}/confirm")
    assert (_db_count("t_return_slip"), _db_count("t_return_line")) == (0, 0)


def test_確定した返品の分だけ次の伝票で返品できる残りが減る(client):
    a = prepare(client, [normal_line(truth={"数量": "8"})], name="a.pdf", text="a")
    assert client.post(f"/api/docs/{a}/confirm").status_code == 200
    b = prepare(client, [normal_line(truth={"数量": "3"})], header={**GOOD_HEADER, "伝票番号": "R-000200"}, name="b.pdf", text="b")
    check = client.get(f"/api/docs/{b}/check").get_json()["check"]
    assert any(i["check"] == "S5" for i in check["issues"])  # 10冊売って8冊返品済み → 残り2冊


def test_チェック結果に返品理由の選択肢がつく(client):
    doc_id = prepare(client, [normal_line()])
    body = client.get(f"/api/docs/{doc_id}/check").get_json()
    assert [r["name"] for r in body["reasons"]][:3] == ["サイズ違い", "色違い", "初期不良"]


def test_OCRの値は最初から採用し要確認の欄だけ採用しない(client):
    import json

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample_slip_ocr.json")
    ocr = json.load(open(fixture, encoding="utf-8"))
    doc_id = upload(client)
    app_module.store.save_ocr(app_module.store.get(doc_id), 1, {"page": 1, "dpi": 200, "width": 1, "height": 1, "items": ocr["items"]})
    rows = client.get(f"/api/docs/{doc_id}/pages/1").get_json()["table"]["rows"]
    first = rows[0]["cells"]
    assert first["品番"]["adopt_ocr"] is False  # 「NOT E- 1 0 41」信頼度0.29 → 要確認
    assert first["数量"]["adopt_ocr"] is True  # 「2」信頼度1.00 → 最初から採用
    assert first["返品理由"]["adopt_ocr"] is True


def _upload_with_fixture(client):
    import json

    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sample_slip_ocr.json")
    ocr = json.load(open(fixture, encoding="utf-8"))
    doc_id = upload(client)
    app_module.store.save_ocr(app_module.store.get(doc_id), 1, {"page": 1, "dpi": 200, "width": 1, "height": 1, "items": ocr["items"]})
    return doc_id


def test_見出しは保存前でもOCRの値で照合し自信の低い項目は確認を求める(client):
    doc_id = _upload_with_fixture(client)
    check = client.get(f"/api/docs/{doc_id}/check").get_json()["check"]
    assert check["header_values"]["伝票番号"] == "R-000123"  # 保存していなくてもOCRの値を使う
    assert check["header"]["会社コード"] == "100001"
    low = [i for i in check["issues"] if i.get("field")]
    assert [i["field"] for i in low] == ["返品日"]  # 信頼度0.44


def test_自信の低い見出しを確認済みにすると求めなくなる(client):
    doc_id = _upload_with_fixture(client)
    header = {**GOOD_HEADER, "確認済み": ["返品日"]}
    body = client.put(f"/api/docs/{doc_id}/header", json=header).get_json()
    assert not [i for i in body["check"]["issues"] if i.get("field")]


# ---- 複数PDFのまとめて読み込みと伝票一覧（要件定義書 F1・F2） ----


def test_複数のPDFをまとめて読み込み読めないものはそのファイルだけエラー(client):
    files = {"pdfs": [(io.BytesIO(make_pdf("a")), "a.pdf"), (io.BytesIO(b"not a pdf"), "b.pdf"), (io.BytesIO(make_pdf("c")), "c.txt")]}
    res = client.post("/api/docs/batch", data=files, content_type="multipart/form-data")
    body = res.get_json()
    assert res.status_code == 200, body
    assert [r["ok"] for r in body["results"]] == [True, False, False]
    assert "PDF" in body["results"][1]["error"]
    assert [d["filename"] for d in body["docs"]] == ["a.pdf"]
    assert body["docs"][0]["status"] == "未OCR"


def test_伝票一覧の状態は要確認とエラーと問題なしに分かれる(client):
    # 要確認：OCRの自信が低い欄が残っている（返品日・品番）
    need = _upload_with_fixture(client)
    client.put(f"/api/docs/{need}/pages/1/review", json={"rows": client.get(f"/api/docs/{need}/pages/1").get_json()["table"]["rows"]})
    # エラー：商品マスタに無い品番
    err = prepare(client, [normal_line(truth={"品番": "NOTE-999"})], header={**GOOD_HEADER, "伝票番号": "R-2"}, name="err.pdf", text="err")
    # 問題なし
    ok = prepare(client, [normal_line()], header={**GOOD_HEADER, "伝票番号": "R-3"}, name="ok.pdf", text="ok")
    rows = {d["doc_id"]: d for d in client.get("/api/docs").get_json()["docs"]}
    assert rows[need]["status"] == "要確認" and rows[need]["need"] >= 1
    assert rows[err]["status"] == "エラー"
    assert rows[ok]["status"] == "問題なし" and rows[ok]["total"] == 480
    assert rows[ok]["partner"] == "株式会社サンプル商事"


def test_問題なしの伝票だけまとめて確定できる(client):
    ok1 = prepare(client, [normal_line()], name="a.pdf", text="a")
    ok2 = prepare(client, [normal_line(truth={"数量": "1"})], header={**GOOD_HEADER, "伝票番号": "R-2"}, name="b.pdf", text="b")
    bad = prepare(client, [normal_line(extra={"良品不良": ""})], header={**GOOD_HEADER, "伝票番号": "R-3"}, name="c.pdf", text="c")
    body = client.post("/api/docs/confirm-all", json={}).get_json()
    assert sorted(body["confirmed"]) == ["a.pdf", "b.pdf"]
    assert body["skipped"] == []  # 問題なしだけを対象にするので、c.pdf は最初から含まれない
    rows = {d["doc_id"]: d for d in body["docs"]}
    assert (rows[ok1]["status"], rows[ok2]["status"], rows[bad]["status"]) == ("確定済み", "確定済み", "要確認")


def test_再起動しても読み込んだPDFが一覧に残る(client):
    doc_id = prepare(client, [normal_line()])
    # サーバーの再起動をまねる：同じ記録フォルダで DocStore を作り直す
    app_module.store = DocStore(app_module.store.records_dir)
    rows = client.get("/api/docs").get_json()["docs"]
    assert [d["doc_id"] for d in rows] == [doc_id]
    assert rows[0]["status"] == "問題なし"


def test_確認中の伝票は一覧から外せるが確定済みは外せない(client):
    a = prepare(client, [normal_line()], name="a.pdf", text="a")
    b = prepare(client, [normal_line()], header={**GOOD_HEADER, "伝票番号": "R-2"}, name="b.pdf", text="b")
    client.post(f"/api/docs/{b}/confirm")
    assert client.delete(f"/api/docs/{a}").status_code == 200
    assert client.delete(f"/api/docs/{b}").status_code == 409
    assert [d["filename"] for d in client.get("/api/docs").get_json()["docs"]] == ["b.pdf"]
