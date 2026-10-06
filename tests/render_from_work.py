"""work/<name>/translations.json (保存済みの訳) だけから日本語PDFを描き直す (Gemini 不要): python tests/render_from_work.py <pdf> <outdir>"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from readable.cli import work_name
from readable.config import load_config
from readable.glossary import load_override, normalize_all
from readable.render import render_ja, save_report
from readable.translate import frames_translations

def main():
    pdf = Path(sys.argv[1]); out = Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
    wd = ROOT / "work" / work_name(pdf)
    doc = json.loads((wd / "doc.json").read_text(encoding="utf-8"))
    t = json.loads((wd / "translations.json").read_text(encoding="utf-8"))
    ja = t["ja"]
    gp = wd / "glossary.json"
    gl = json.loads(gp.read_text(encoding="utf-8")).get("glossary", []) if gp.exists() else []
    ov = load_override(wd / "glossary_override.json")
    gl = ov if ov is not None else gl
    ja, cnt = normalize_all(ja, gl)
    print("normalized", cnt)
    fr = frames_translations(doc, t["units"], ja)
    rep = render_ja(pdf, doc, fr, out / f"{pdf.stem}_ja.pdf", cfg=load_config())
    save_report(rep, out / "render_report.json")
    print({k: rep[k] for k in ("frames", "shrunk", "shrunk_ratio", "min_scale", "failed", "expanded", "stages")})
    for w in rep["warnings"]: print("WARN", w)
main()
