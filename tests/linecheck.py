"""出力 PDF の行頭禁則違反 (行頭の 。、）」など) と 1 文字だけの行を数える (render の後検査と同じ判定)。"""
from __future__ import annotations
import re, sys
import fitz
BAD_START = "。、，．）」』】〉》］｝・ー！？)]}!?,."   # 行頭に来てはいけない文字 (ー は長音なので行頭禁則)
CJK = re.compile("[぀-ヿ㐀-鿿]")

def scan(pdf):
    d = fitz.open(pdf); res = []
    for pno, pg in enumerate(d, 1):
        for b in pg.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                t = "".join(s["text"] for s in l["spans"]).strip("  ")
                if not CJK.search(t) and not any(c in BAD_START for c in t[:1]):
                    continue
                if t and t[0] in "。、，．）」』】〉》］｝・ー！？":
                    res.append((pno, "start", t[:12]))
                elif len(t) == 1 and CJK.search(t):
                    res.append((pno, "single", t))
    return res
if __name__ == "__main__":
    for f in sys.argv[1:]:
        r = scan(f); print(f, len(r)); [print("  ", x) for x in r[:40]]
