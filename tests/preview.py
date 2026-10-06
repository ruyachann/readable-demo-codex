"""出力PDFをページごとに PNG に変換する: python tests/preview.py out/x_ja.pdf [--dpi 80] [--out out/preview] [--pages 1,3]"""
from __future__ import annotations

import argparse
from pathlib import Path

import fitz


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=80)
    ap.add_argument("--out", default="out/preview")
    ap.add_argument("--pages", default="", help="例: 1,3,6 (1始まり)。省略で全ページ")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(a.pdf)
    sel = [int(x) for x in a.pages.split(",") if x] or list(range(1, len(doc) + 1))
    stem = Path(a.pdf).stem
    for n in sel:
        pix = doc[n - 1].get_pixmap(dpi=a.dpi)
        p = out / f"{stem}_p{n:02d}.png"
        pix.save(str(p))
        print(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
