"""Generate fictional examples through the app renderer, without any AI calls."""
from pathlib import Path
import sys, json, re
SITE = Path(__file__).resolve().parents[1]
ROOT = SITE.parent
sys.path.insert(0, str(ROOT))
import fitz
from readable.config import load_config
from readable.extract import extract_pdf
from readable.render import render_ja, build_dual
ASSETS = SITE / "dist" / "assets"
def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    original = ASSETS / "sample-en.pdf"
    ja_path = ASSETS / "sample-ja.pdf"
    dual_path = ASSETS / "sample-dual.pdf"
    doc = fitz.open()
    p = doc.new_page(width=595, height=842)
    ink=(.08,.17,.22)
    green=(.17,.45,.36)
    pairs=[]
    def block(y, height, text, ja, size=11, font="tiro", role="body"):
        rect=fitz.Rect(55,y,540,y+height)
        result=p.insert_textbox(rect,text,fontsize=size,fontname=font,lineheight=1.5,color=ink)
        if result < 0: raise RuntimeError("Sample text did not fit")
        pairs.append((text,ja,role))
    p.insert_text((55,40),"PDF TRANSLATE  /  SAMPLE RESEARCH NOTE",fontsize=8,fontname="helv",color=green)
    p.draw_line((55,51),(540,51),color=(.73,.79,.79),width=.5)
    block(73,45,"Reading with less friction","読みやすさを支える文書のかたち",23,"tibo","title")
    block(119,35,"A fictional study of document reading environments","文書の読書環境に関する架空の研究メモ",12,"tiro","heading")
    p.insert_text((55,158),"Sample Research Group  |  Fictional data for illustration",fontsize=8.5,fontname="helv",color=(.4,.47,.49))
    block(178,28,"Abstract","要旨",12,"tibo","heading")
    block(207,72,"This fictional study explores how page layout supports the reading of academic documents. Readers compared a text-only view with a layout-preserving view. The example illustrates document formatting and does not report results from a real experiment.","この架空の研究では、学術文書を読む際にページの配置がどのように理解を支えるかを検討する。読者は文章のみの表示と、配置を保った表示を比較した。本例は文書の体裁を示すためのもので、実際の実験結果を報告するものではない。",11,"tiro","abstract")
    block(300,28,"1. Methods","1. 方法",12,"tibo","heading")
    block(333,73,"Two reading environments were prepared with the same paragraphs and figures. The layout-preserving view placed each figure near its caption. We used fictional scores to visualize the difference between these environments.","同じ段落と図を使い、2種類の読書環境を用意した。配置を保つ表示では、図をそのキャプションの近くに置いた。環境の違いを可視化するため、架空のスコアを用いた。",11,"tiro","body")
    # A functional plot of explicitly fictional scores, preserved by the renderer.
    p.draw_rect(fitz.Rect(72,432,523,621),fill=(.95,.97,.97),color=(.86,.9,.91),width=.5)
    p.insert_text((90,455),"Fictional reading score",fontsize=9,fontname="helv",color=ink)
    for y,value in [(486,100),(522,50),(558,0)]:
        p.draw_line((122,y),(499,y),color=(.8,.85,.86),width=.4)
        p.insert_text((96,y+3),str(value),fontsize=8,fontname="helv",color=(.45,.5,.52))
    p.draw_rect(fitz.Rect(178,518,267,558),fill=(.59,.66,.69),color=None)
    p.draw_rect(fitz.Rect(347,497,436,558),fill=(.3,.62,.5),color=None)
    p.insert_text((185,578),"Text-only view",fontsize=8.5,fontname="helv",color=ink)
    p.insert_text((342,578),"Layout-preserving",fontsize=8.5,fontname="helv",color=ink)
    p.insert_text((184,608),"All values are fictional.",fontsize=8,fontname="helv",color=(.42,.49,.5))
    block(637,42,"Figure 1. Fictional scores in two reading environments. The original chart remains unchanged in the Japanese PDF.","図1. 2種類の読書環境における架空のスコア。日本語版PDFでも、元のグラフは変更せずに保持する。",10,"tiro","caption")
    block(698,28,"2. Discussion","2. 考察",12,"tibo","heading")
    block(731,58,"Keeping text close to its figures may help readers follow the structure of a document. This sample is intended only to demonstrate how the translated text fits into a PDF page.","文章と図を近くに配置すると、文書の構造を追いやすくなる可能性がある。このサンプルは、翻訳した文章をPDFのページ内に配置する方法を示すためだけに作成した。",10.5,"tiro","body")
    p.insert_text((55,817),"ILLUSTRATIVE SAMPLE  /  NOT A REAL PUBLICATION",fontsize=7,fontname="helv",color=(.45,.5,.52))
    p.insert_text((535,817),"1",fontsize=8,fontname="helv",color=ink)
    doc.save(original,garbage=4,deflate=True)
    doc.close()
    cfg=load_config()
    extracted=extract_pdf(original,cfg=cfg)
    translations={}
    norm=lambda s: re.sub(r"\s+"," ",s).strip()
    for frame in extracted["pages"][0]["frames"]:
        frame["translate"]=False
        ft=norm(frame["text"])
        for en,ja,role in pairs:
            if ft == norm(en):
                frame["translate"]=True
                frame["role"]=role
                translations[frame["id"]]=ja
                break
    if len(translations)!=len(pairs):
        print(json.dumps([{"text":f["text"],"bbox":f["bbox"]} for f in extracted["pages"][0]["frames"]],ensure_ascii=True))
        raise RuntimeError(f"Matched {len(translations)} of {len(pairs)} sample blocks")
    report=render_ja(original,extracted,translations,ja_path,cfg=cfg)
    if report["failed"]: raise RuntimeError("Sample rendering failed")
    build_dual(original,ja_path,dual_path)
    for name,path in [("en",original),("ja",ja_path)]:
        with fitz.open(path) as d:
            d[0].get_pixmap(matrix=fitz.Matrix(900/595,900/595),alpha=False).save(ASSETS/f"sample-{name}.png")
    (SITE/"sample_report.json").write_text(json.dumps({"fictional":True,"ai_calls":0,"manual_translation":True,"render_report":report},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"sample_blocks":len(translations),"render_failed":report["failed"],"ai_calls":0}))
if __name__=="__main__": main()


