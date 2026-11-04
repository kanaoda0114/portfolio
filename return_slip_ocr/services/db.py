"""返品処理のデータベース（SQLite）。テーブルは docs/データ設計書.md のとおり。

SQLite は Python に最初から入っているので、追加のサーバーやインストールは不要。
データベースはこのPCの records/returns.db に保存され、外部には送信しない。
"""
import os
import sqlite3

from .errors import AppError

SCHEMA = """
-- 3.1 取引先マスタ
CREATE TABLE IF NOT EXISTS m_partner (
    partner_code  TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    name_kana     TEXT,
    aliases       TEXT,
    invoice_no    TEXT,
    closing_day   INTEGER NOT NULL CHECK (closing_day BETWEEN 1 AND 28 OR closing_day = 99),
    payment_site  TEXT NOT NULL,
    tax_rounding  TEXT NOT NULL CHECK (tax_rounding IN ('切り捨て', '四捨五入', '切り上げ')),
    active        INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL
);

-- 3.2 商品マスタ
CREATE TABLE IF NOT EXISTS m_item (
    item_no     TEXT NOT NULL,
    branch_no   TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL,
    unit        TEXT,
    tax_rate    INTEGER NOT NULL CHECK (tax_rate IN (8, 10)),
    active      INTEGER NOT NULL DEFAULT 1,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (item_no, branch_no)
);

-- 3.3 返品理由マスタ
CREATE TABLE IF NOT EXISTS m_return_reason (
    reason_code  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    aliases      TEXT,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    active       INTEGER NOT NULL DEFAULT 1
);

-- 3.4 売上・売上明細（販売管理システムから取り込む。このツールでは変更しない）
CREATE TABLE IF NOT EXISTS t_sales (
    sales_no      TEXT PRIMARY KEY,
    partner_code  TEXT NOT NULL REFERENCES m_partner (partner_code),
    sales_date    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS t_sales_line (
    sales_no    TEXT NOT NULL REFERENCES t_sales (sales_no) ON DELETE CASCADE,
    line_no     INTEGER NOT NULL,
    item_no     TEXT NOT NULL,
    branch_no   TEXT NOT NULL DEFAULT '',
    qty         INTEGER NOT NULL CHECK (qty > 0),
    unit_price  NUMERIC NOT NULL,
    amount      INTEGER NOT NULL,
    PRIMARY KEY (sales_no, line_no),
    FOREIGN KEY (item_no, branch_no) REFERENCES m_item (item_no, branch_no)
);
CREATE INDEX IF NOT EXISTS ix_sales_line_item ON t_sales_line (item_no, branch_no);

-- 3.5 返品伝票・返品明細（このツールで確定した返品）
CREATE TABLE IF NOT EXISTS t_return_slip (
    return_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_code  TEXT NOT NULL REFERENCES m_partner (partner_code),
    slip_no       TEXT NOT NULL,
    return_date   TEXT NOT NULL,
    fiscal_month  TEXT NOT NULL,
    closing_date  TEXT NOT NULL,
    late          INTEGER NOT NULL DEFAULT 0,
    source_file   TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('確定済み', '出力済み')),
    confirmed_at  TEXT NOT NULL,
    exported_at   TEXT,
    UNIQUE (partner_code, slip_no)
);

CREATE TABLE IF NOT EXISTS t_return_line (
    return_id      INTEGER NOT NULL REFERENCES t_return_slip (return_id) ON DELETE CASCADE,
    line_no        INTEGER NOT NULL,
    line_kind      TEXT NOT NULL CHECK (line_kind IN ('通常', '値引')),
    sales_no       TEXT,
    sales_line_no  INTEGER,
    item_no        TEXT,
    branch_no      TEXT,
    qty            INTEGER NOT NULL,
    unit_price     NUMERIC NOT NULL,
    amount         INTEGER NOT NULL,
    tax_mode       TEXT NOT NULL CHECK (tax_mode IN ('10', '8', '按分')),
    tax_rate       INTEGER,
    reason_code    TEXT NOT NULL REFERENCES m_return_reason (reason_code),
    reason_text    TEXT,
    condition      TEXT CHECK (condition IN ('良品', '不良')),
    PRIMARY KEY (return_id, line_no),
    FOREIGN KEY (sales_no, sales_line_no) REFERENCES t_sales_line (sales_no, line_no)
);
CREATE INDEX IF NOT EXISTS ix_return_line_sales ON t_return_line (sales_no, sales_line_no);
"""


def connect(path):
    """データベースを開く（無ければ作る）。外部キーのチェックを必ず有効にする。"""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        con = sqlite3.connect(path)
    except (OSError, sqlite3.Error) as e:
        raise AppError(
            f"データベースを開けません: {path}（{e}）",
            "records フォルダに書き込めるか確認してください。",
            status=500,
            code="db_open",
        )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def counts(con):
    """各テーブルの件数（画面での表示・確認用）。"""
    names = ["m_partner", "m_item", "m_return_reason", "t_sales", "t_sales_line", "t_return_slip", "t_return_line"]
    return {n: con.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names}
