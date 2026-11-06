"""伝票の画像から、明細表の縦の罫線（列の境界）を見つける。

列の境界を「見出しの文字の中間」で決めると、右寄せの数字（数量・単価）が境界をまたいで
隣の列と区別できないことがある。実際の伝票には罫線があるので、罫線が見つかればそれを境界にする。
罫線が無い伝票では空のリストを返し、今までどおり見出しの中間で区切る。
"""
import cv2
import numpy as np


def vertical_lines(image, min_length_ratio=0.08):
    """縦の罫線の x 座標（ピクセル）を左から順に返す。

    min_length_ratio: 画像の高さに対する、罫線とみなす最小の長さ（明細表の高さは紙の1〜2割程度）
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if image.ndim == 3 else image
    h, w = gray.shape[:2]
    # 周りより暗いところを黒にする（薄い罫線・スキャンで全体が暗い紙でも拾える）
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    # 縦に長い黒いところだけ残す（文字は縦に短いので消える）。少し傾いた線も拾えるよう幅5px
    min_len = max(int(h * min_length_ratio), 20)
    # 文字（縦に短い）を消すには短い縦カーネルで十分。傾いた線も途切れないよう、先に横へ少し太らせる
    short = max(int(h * 0.02), 15)
    opened = cv2.morphologyEx(cv2.dilate(binary, np.ones((1, 5), np.uint8)), cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, short)))
    # 傾いた線は x が少しずつずれるので、横に広げてから「縦にどれだけ黒いか」を数える
    opened = cv2.dilate(opened, np.ones((1, 9), np.uint8))
    cols = opened.sum(axis=0) / 255  # 各 x で縦に何ピクセル黒いか
    xs = np.where(cols >= min_len)[0]
    # 隣り合う x をまとめて1本の線にする
    lines = []
    for x in xs:
        if lines and x - lines[-1][-1] <= 12:
            lines[-1].append(int(x))
        else:
            lines.append([int(x)])
    return [round(sum(g) / len(g), 1) for g in lines]


def column_bounds(center_x, lines):
    """見出しの中心 x に対して、左右いちばん近い罫線を返す（無い側は None）。"""
    left = max((x for x in lines if x < center_x), default=None)
    right = min((x for x in lines if x > center_x), default=None)
    return left, right
