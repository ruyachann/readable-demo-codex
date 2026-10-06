"""出力 PDF の重なり検査: 日本語の行 bbox が (a) 翻訳しない frame・画像、(b) 他の日本語行 と重なる件数を数える。"""
from __future__ import annotations

import re

import fitz

CJK = re.compile(r"[぀-ヿ一-鿿]")


def count_overlaps(doc: dict, out_pdf, verbose: bool = False) -> int:
    out = fitz.open(str(out_pdf))
    nbad = 0
    for p in doc["pages"]:
        pg = out[p["number"] - 1]
        M = pg.rotation_matrix
        lines = []
        for b in pg.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for l in b["lines"]:
                t = "".join(s["text"] for s in l["spans"])
                if CJK.search(t):
                    r = (fitz.Rect(l["bbox"]) * M).normalize()  # 見た目の座標へ
                    lines.append(([r.x0, r.y0, r.x1, r.y1], t))
        fixed = [(f["id"], f["bbox"]) for f in p["frames"] if not f["translate"]] + [("img", b) for b in p["images"]]
        for bb, t in lines:
            for oid, ob in fixed:
                w = min(bb[2], ob[2]) - max(bb[0], ob[0])
                h = min(bb[3], ob[3]) - max(bb[1], ob[1])
                if w > 1 and h > 1.5:
                    nbad += 1
                    if verbose:
                        print("  JA over non-translated", p["number"], oid, round(w), round(h, 1), t[:20])
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i][0], lines[j][0]
                w = min(a[2], b[2]) - max(a[0], b[0])
                h = min(a[3], b[3]) - max(a[1], b[1])
                if w > 2 and h > 2.0:
                    nbad += 1
                    if verbose:
                        print("  JA/JA overlap", p["number"], round(w), round(h, 1), lines[i][1][:10], lines[j][1][:10])
    out.close()
    return nbad
