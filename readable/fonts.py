"""日本語フォントの解決と fitz.Archive / CSS の生成 (文書ごとに1回だけ)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import fitz

from .config import Config, load_config

PROBE_TEXT = "日本語テスト 游明朝 ABC 123"


@dataclass
class FontSet:
    archive: fitz.Archive
    css: str
    files: dict[str, str] = field(default_factory=dict)  # family-style -> 採用ファイル
    notes: list[str] = field(default_factory=list)

    def family(self, serif: bool) -> str:
        return "jpserif" if serif else "jpsans"


@lru_cache(maxsize=None)
def _probe(path: Path, bold: bool) -> bool:
    """そのフォントファイルが insert_htmlbox で実際に埋め込まれるか試す。"""
    try:
        data = path.read_bytes()
        arch = fitz.Archive()
        arch.add(data, "probe" + path.suffix)
        css = "@font-face{font-family:probe;%ssrc:url(probe%s)}" % ("font-weight:bold;" if bold else "", path.suffix)
        doc = fitz.open()
        page = doc.new_page(width=300, height=100)
        html = '<div style="font-family:probe;font-size:12pt;%s">%s</div>' % ("font-weight:bold;" if bold else "", PROBE_TEXT)
        spare, _ = page.insert_htmlbox(fitz.Rect(5, 5, 295, 95), html, css=css, archive=arch, scale_low=1)
        if spare < 0:
            return False
        names = [f[3] for f in page.get_fonts()]
        txt = page.get_text()
        doc.close()
        return bool(names) and "日本語" in txt and not any("Droid" in n for n in names)
    except Exception:
        return False


def _pick(cands: list[str], bold: bool, notes: list[str]) -> Path | None:
    for c in cands:
        p = Path(c)
        if not p.exists():
            notes.append(f"missing: {c}")
            continue
        if _probe(p, bold):
            return p
        notes.append(f"probe failed: {c}")
    return None


def build_fonts(cfg: Config | None = None) -> FontSet:
    cfg = cfg or load_config()
    fc = cfg.section("fonts")
    notes: list[str] = []
    arch = fitz.Archive()
    css_parts: list[str] = []
    files: dict[str, str] = {}
    for fam, reg_key, bold_key in (("jpserif", "serif_regular", "serif_bold"), ("jpsans", "sans_regular", "sans_bold")):
        reg = _pick(fc.get(reg_key, []), False, notes)
        bld = _pick(fc.get(bold_key, []), True, notes)
        if reg is None:
            raise RuntimeError(f"フォントが見つかりません ({reg_key}): {fc.get(reg_key)}  notes={notes}")
        arch.add(reg.read_bytes(), f"{fam}_r{reg.suffix}")
        css_parts.append("@font-face{font-family:%s;src:url(%s_r%s)}" % (fam, fam, reg.suffix))
        files[f"{fam}-regular"] = str(reg)
        if bld is not None:
            arch.add(bld.read_bytes(), f"{fam}_b{bld.suffix}")
            css_parts.append("@font-face{font-family:%s;font-weight:bold;src:url(%s_b%s)}" % (fam, fam, bld.suffix))
            files[f"{fam}-bold"] = str(bld)
        # 斜体 (統計記号 p, t, U, d や英単語の <i>) 用のラテン斜体。日本語フォントに斜体面が無いので別ファイルを斜体面として登録する
        # (和文は斜体にしない。render が <i> 内の和文を外す)。見つからなければ斜体は立体のまま。
        for it in fc.get("serif_italic" if fam == "jpserif" else "sans_italic", []):
            ip = Path(it)
            if ip.exists():
                arch.add(ip.read_bytes(), f"{fam}_i{ip.suffix}")
                css_parts.append("@font-face{font-family:%s;font-style:italic;src:url(%s_i%s)}" % (fam, fam, ip.suffix))
                files[f"{fam}-italic"] = str(ip)
                break
    return FontSet(archive=arch, css="".join(css_parts) + "a{color:inherit;text-decoration:none}", files=files, notes=notes)
