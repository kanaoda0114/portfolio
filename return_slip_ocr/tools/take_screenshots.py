"""README 用の画面スクリーンショットを撮る（Issue #21）。

使い方（return_slip_ocr フォルダで。アプリを http://127.0.0.1:5000 で起動し、伝票を何枚か読み込んでおく）:
    .venv\\Scripts\\pip install playwright
    .venv\\Scripts\\python -m playwright install chromium
    .venv\\Scripts\\python tools\\take_screenshots.py

docs/screenshots/ に 3 枚保存する:
    01_list.png     伝票一覧（読み込み・状態の確認）
    02_review.png   作業画面（左に伝票、右に読み取り結果と入力欄）
    03_masters.png  マスタ・売上データの取り込み
Playwright は開発時だけ使うので requirements.txt には入れていない。
"""
import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "screenshots")
URL = os.environ.get("APP_URL", "http://127.0.0.1:5000/")


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        page.goto(URL)
        page.wait_for_selector("text=伝票一覧")
        page.wait_for_timeout(1500)
        page.screenshot(path=os.path.join(OUT, "01_list.png"))

        page.get_by_role("button", name="開く").first.click()
        page.wait_for_timeout(2500)
        page.screenshot(path=os.path.join(OUT, "02_review.png"))

        page.goto(URL + "#step-masters")
        page.wait_for_timeout(1500)
        section = page.locator("#master-list").locator("xpath=ancestor::section[1]")
        section.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        section.screenshot(path=os.path.join(OUT, "03_masters.png"))  # この区画だけ切り出す
        browser.close()
    for name in sorted(os.listdir(OUT)):
        print(os.path.join(OUT, name))


if __name__ == "__main__":
    sys.exit(main())
