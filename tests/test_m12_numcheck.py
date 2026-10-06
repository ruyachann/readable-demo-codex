"""M12: Japanese comparison negation, using synthetic text only."""
import pytest

from readable.numcheck import _plain, check_numbers
from readable.translate import check_translation


@pytest.mark.parametrize("check", [check_numbers, check_translation])
@pytest.mark.parametrize("phrase,expected", [
    ("より大きくない", "<="),
    ("より高くない", "<="),
    ("より多くない", "<="),
    ("を超えない", "<="),
    ("を上回らない", "<="),
    ("より小さくない", ">="),
    ("より低くない", ">="),
    ("より少なくない", ">="),
    ("を下回らない", ">="),
    ("を下まわらない", ">="),
    ("以上ではない", "<"),
    ("以下ではない", ">"),
    ("未満ではない", ">="),
    ("超ではない", "<="),
    ("より大きくありません", "<="),
    ("より小さくなかった", ">="),
    ("を超えません", "<="),
    ("を上回りません", "<="),
    ("を下回らなかった", ">="),
    ("以上ではありません", "<"),
    ("以下でない", ">"),
])
def test_negated_comparisons_preserve_direction_and_boundary(check, phrase, expected):
    ja = f"p値は0.05{phrase}。"
    for op in ("<", "<=", ">", ">="):
        problems = check(f"p {op} 0.05", ja)
        if op == expected:
            assert problems == []
        else:
            assert len(problems) == 1 and "比較演算子" in problems[0], (op, ja, problems)


@pytest.mark.parametrize("check", [check_numbers, check_translation])
@pytest.mark.parametrize("phrase,expected", [
    ("より大きい", ">"), ("より高い", ">"), ("より多い", ">"),
    ("を超える", ">"), ("を上回る", ">"),
    ("より小さい", "<"), ("より低い", "<"), ("より少ない", "<"),
    ("を下回る", "<"), ("を下まわる", "<"),
    ("以上", ">="), ("以下", "<="), ("未満", "<"), ("超", ">"),
])
def test_positive_comparisons_remain_valid(check, phrase, expected):
    assert check(f"p {expected} 0.05", f"p値は0.05{phrase}。") == []


@pytest.mark.parametrize("check", [check_numbers, check_translation])
def test_negation_is_local_to_each_numeric_comparison(check):
    src = "p ≤ 0.05; q ≥ 0.01; r > 0.02"
    ja = "p値は0.05より大きくない。q値は0.01を下回らない。r値は0.02を上回る。"
    assert check(src, ja) == []
    assert any("比較演算子" in p for p in check(src, ja.replace("上回る", "上回らない")))


@pytest.mark.parametrize("src", [
    "p < 0.05; q > 0.01",
    "p<0.05; q>0.01",
    "p <= 0.05; q >= 0.01",
])
def test_plain_preserves_raw_numeric_comparisons(src):
    assert _plain(src) == src


@pytest.mark.parametrize("check", [check_numbers, check_translation])
@pytest.mark.parametrize("src", [
    "p < 0.05; q > 0.01",
    "p<0.05; q>0.01",
    '<b><i>p</i></b> < 0.05; <a href="https://example.invalid/2026?q=1">q</a> > 0.01',
    '<b><i>p</i></b> &lt; 0.05; <a href="https://example.invalid/2026?q=1">q</a> &gt; 0.01',
])
def test_raw_comparisons_cannot_bypass_validation(check, src):
    # Preserve the source's real tags and link while changing only the comparisons.
    assert check(src, src) == []
    flipped = src.replace("< 0.05", "> 0.05").replace("<0.05", ">0.05").replace("&lt;", "&gt;")
    assert any("比較演算子" in p for p in check(src, flipped))
    omitted = src.replace("0.05", "")
    assert any("数値が欠落/変化" in p for p in check(src, omitted))


@pytest.mark.parametrize("markup", [
    '<b><i>p</i></b> &lt; 0.05; <a href="https://example.invalid/2026?q=1">q</a> &gt; 0.01',
    '<b><i>p</i></b> &#60; 0.05; <a href="https://example.invalid/2026?q=1">q</a> &#x3e; 0.01',
    '<b><i>p</i></b> < 0.05; <a href="https://example.invalid/2026?q=1&gt;0">q</a> > 0.01',
    '<b><i>p</i></b> < 0.05; <a href="https://example.invalid/2026?q=1>0">q</a> > 0.01',
])
def test_plain_strips_real_tags_and_link_attributes_but_keeps_entities(markup):
    assert _plain(markup) == "p < 0.05; q > 0.01"


@pytest.mark.parametrize("check", [check_numbers, check_translation])
@pytest.mark.parametrize("exponent", ["-5", "−5", "⁻⁵"])
def test_superscript_exponents_survive_raw_comparisons(check, exponent):
    src = f"p < 1.2 × 10<sup>{exponent}</sup>; q > 0.01"
    ja = f"p値は1.2×10<sup>{exponent}</sup>未満、q値は0.01超。"
    assert _plain(src) == "p < 1.2 × 10^-5; q > 0.01"
    assert check(src, ja) == []
    changed = ja.replace(f"<sup>{exponent}</sup>", "<sup>-4</sup>")
    assert any("数値が欠落/変化" in p for p in check(src, changed))
