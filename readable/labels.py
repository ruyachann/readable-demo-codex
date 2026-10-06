"""定型ラベル (サイドバー・ページ上部の「Received:」「Correspondence」など) の辞書訳。API を使わず決定的に訳す。

日付・氏名・助成番号・メールなどの値は原文のまま残し、ラベルだけを日本語にする。対象は role が sidebar / page_header の frame で、
先頭がラベル (後ろにコロン) か、frame 全体がラベルだけのもの。
"""
from __future__ import annotations

import re

# 論文の種類の札 (ARTICLE / OPEN / RESEARCH ARTICLE など) はここに入れない: 誌名のロゴの一部として全て原文のままにする (M13。以前は「論文」などに訳していた)
LABELS: dict[str, str] = {
    "received": "受付",
    "revised": "改訂",
    "accepted": "受理",
    "published": "公開",
    "available online": "オンライン公開",
    "correspondence": "連絡先",
    "corresponding author": "責任著者",
    "funding information": "研究資金",
    "funding": "研究資金",
    "academic editor": "担当編集者",
    "academic editors": "担当編集者",
    "handling editor": "担当編集者",
    "editor": "編集者",
    "copyright": "著作権",
    "keywords": "キーワード",
    "email": "メール",
    "e-mail": "メール",
    "citation": "引用",
    "revised version received": "改訂版受付",
    "for correspondence": "連絡先",
    "competing interest": "競合する利益",
    "competing interests": "競合する利益",
    "sent for review": "査読に送付",
    "preprint posted": "プレプリント公開",
    "reviewed preprint posted": "査読付きプレプリント公開",
    "reviewed preprint revised": "査読付きプレプリント改訂",
    "version of record published": "掲載版公開",
    "reviewing editor": "査読編集者",
    "senior editor": "上級編集者",
    "data availability": "データ利用可能性",
    "ethics": "倫理",
}
#: 要旨の小見出し・定番の節見出し (frame 全体がこの語だけのとき、英語のまま残っていたら辞書で訳す。M15: PLOS の要旨の小見出し)
HEADINGS: dict[str, str] = {
    "background": "背景", "objectives": "目的", "study objectives": "研究の目的", "aim": "目的", "aims": "目的", "purpose": "目的",
    "methods": "方法", "method": "方法", "materials and methods": "材料と方法", "results": "結果", "findings": "結果",
    "conclusions": "結論", "conclusion": "結論", "discussion": "考察", "introduction": "はじめに", "abstract": "要旨", "summary": "要旨",
    "author contributions": "著者の貢献", "acknowledgments": "謝辞", "acknowledgements": "謝辞", "references": "参考文献",
    "supporting information": "補足情報", "data availability": "データの利用可能性", "limitations": "限界", "implications": "含意",
}
LABEL_ROLES = {"sidebar", "page_header"}
_ALT = "|".join(re.escape(k) for k in sorted(LABELS, key=len, reverse=True))
_RE = re.compile(r"^((?:<[^>]+>|\s|\*)*)(" + _ALT + r")((?:\s*[:：])?)((?:</[^>]+>)*)(.*)$", re.I | re.S)


def label_override(role: str, html: str) -> str | None:
    """辞書訳したラベル + 原文のままの値 (html) を返す。対象でなければ None。"""
    plain = re.sub(r"<[^>]+>", "", html).strip().rstrip(":.").strip()
    if role in ("heading", "abstract", "body", "caption") and plain.lower() in HEADINGS and len(html) < 80:
        return HEADINGS[plain.lower()]            # 要旨の小見出し・節見出し (bold の 1 語など)。訳されずに残ったときの辞書訳
    if role not in LABEL_ROLES:
        return None
    m = _RE.match(html.strip())
    if not m:
        return None
    head, label, colon, close, rest = m.groups()
    if rest.strip() == "" and not colon:
        return head + LABELS[label.lower()] + close
    if colon and (rest == "" or rest[:1].isspace() or rest[:1] == "<"):
        return head + LABELS[label.lower()] + ":" + close + rest
    return None
