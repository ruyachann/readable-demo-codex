"""charmap / ハイフン解除 / 上付き下付き判定 / 字間空白"""
from conftest import frames_of
from readable.extract import (DEFAULT_CHARMAP, Vocab, apply_charmap, classify_script, despace_letters,
                              hyphen_decision, join_lines)


def test_charmap_degree_minus():
    assert apply_charmap("45\x03 ± 52\x03", "AdvP4C4E74") == "45° ± 52°"
    assert apply_charmap("(\x014.6%)") == "(−4.6%)"
    assert apply_charmap("session \x05 stimulus") == "session × stimulus"


def test_charmap_symbol_font_only():
    f = "AdvP4C4E74"
    assert apply_charmap("Response \x04 1", f) == "Response ~ 1"
    assert apply_charmap("þ", f) == "+"
    assert apply_charmap("ð", f) == "(" and apply_charmap("Þþ", f) == ")+"
    assert apply_charmap("j", f) == "|"
    # 通常フォントの þ ð j は触らない
    assert apply_charmap("Þórr j", "AdvTTa9c1b374") == "Þórr j"


def test_charmap_override_and_unmapped():
    un: dict = {}
    assert apply_charmap("a\x02b", "AdvP4C4E59", None, un) == "ab" and un == {"\x02": 1}
    cm = dict(DEFAULT_CHARMAP)
    cm["\x02"] = "Ö"
    assert apply_charmap("\x02E.", "AdvP4C4E59", cm) == "ÖE."
    assert apply_charmap("\x03", "X", {"\x03": "℃"}) == "℃"


def test_hyphenation_with_vocab():
    v = Vocab.from_texts(["participants were recruited", "the closed-loop CL-TMR protocol",
                          "Ferrara and Michele", "oscillations"])
    assert hyphen_decision("partici", "pants", v) == "join"
    assert hyphen_decision("CL", "TMR", v) == "keep"          # 複合語は文書内にハイフン付きで存在
    assert hyphen_decision("non", "sense", v) == "join"       # 曖昧で次が小文字 -> 連結
    assert hyphen_decision("Fer", "rara", v) == "join"
    assert hyphen_decision("CL", "Group", v) == "keep"        # 大文字始まりで根拠なし -> ハイフンを残す
    assert join_lines(["were partici-", "pants who", "and CL-", "TMR was"], v) == "were participants who and CL-TMR was"
    assert join_lines(["oscilla-", "tions (SOs)"], v) == "oscillations (SOs)"


def test_despace_letters():
    assert despace_letters("R E S E A R C H A R T I C L E") == "RESEARCH ARTICLE"
    assert despace_letters("K E Y W O R D S") == "KEYWORDS"
    assert despace_letters("A normal sentence here") == "A normal sentence here"


def test_script_classification():
    # 上付き: flags&1 (著者所属番号)
    assert classify_script(8.5, 5, 100.0, 12.0, 100.0) == "sup"
    # 下付き: サイズ小 + 基線より下 (JSR p_corrected, size 5.7, +1.6pt)
    assert classify_script(5.7, 4, 101.6, 8.0, 100.0) == "sub"
    # 小型大文字 "ET AL." (4.9pt, 基線が同一) は下付きではない
    assert classify_script(4.9, 4, 100.0, 7.0, 100.0) is None
    # † (6pt, 基線上) は通常
    assert classify_script(6.0, 4, 100.0, 10.0, 100.0) is None
    # フラグ無しでも基線より上のサイズ小 -> 上付き
    assert classify_script(5.0, 4, 97.0, 8.0, 100.0) == "sup"


def test_script_in_real_docs(jsr, mdpi):
    jf, mf = frames_of(jsr), frames_of(mdpi)
    assert any("Federico Salfi<sup>1</sup>" in f["html"] for f in jf.values())
    assert any("Domenico Corigliano<sup>1,2</sup>" in f["html"] for f in jf.values())
    assert any("<sub>corrected</sub>" in f["html"] for f in jf.values())          # 下付き (flags では検出不能)
    assert not any("<sub>ET AL" in f["html"] or "<sub>AL" in f["html"] for f in jf.values())  # 小型大文字の誤検出なし
    assert any("m<sup>2</sup>" in f["html"] for f in mf.values())
    assert any("iPad<sup>®</sup>" in f["html"] for f in mf.values())
    assert any("<sub>" in f["html"] for f in mf.values())
    assert any("<i>Aonyx cinereus</i>" in f["html"] for f in mf.values())         # 斜体


def test_degree_and_minus_in_real_doc(jsr):
    txt = " ".join(f["text"] for f in frames_of(jsr).values())
    assert "°" in txt and "(−4.6%)" in txt and "\x03" not in txt and "\x01" not in txt
    assert "Response ~ 1 + stimulus type" in txt
