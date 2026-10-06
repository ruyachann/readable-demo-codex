"""数値検証 (CR-05/06): 原文と訳文の「数値表現」を、符号・比較演算子・範囲・科学表記・数量単位まで含めて照合する。

数値トークン = (比較演算子, 符号, 値)。値は Decimal に正規化する:
- 千位区切りのカンマ・全角数字 (NFKC) を除く。
- 数量の言い換え: 原文の `5 million` と訳文の `500万` は、どちらも 5000000 に換算して一致とみなす
  (英: thousand/million/billion/trillion、和: 千/万/億/兆。数字の直後にあるときだけ数量単位。「千葉」「百瀬」は数値に付かないので無関係)。
- 科学表記: `1.2 × 10^-5` / `1.2e-5` / `1.2×10<sup>-5</sup>` は値に換算する。
- 負号 (− - –) は、直前が数字・英字でなく空白・開き括弧・演算子・和文などのときだけ負号とみなす (2019-2020 の - は範囲)。
- 比較演算子 (< > ≤ ≥ = ≠、訳文では 未満/以下/超/以上 も) は、数値の直前 (空白や p・n などの 1 語を挟んでもよい) または直後にあるものを対応づける。
訳文側に原文に無い数値があれば「余分な数値」として指摘する (three → 3 のような数詞の換算、1〜10 の個数表現 (2つ など) は許す)。
"""
from __future__ import annotations

import html as htmllib
import re
import unicodedata
from collections import Counter
from decimal import Decimal, InvalidOperation

NUM_VALIDATION_VERSION = 3  # 比較の否定形と、生の < / > を保持する HTML 除去

_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
          "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
          "eighty": 80, "ninety": 90, "hundred": 100, "dozen": 12, "half": None,
          "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
          "tenth": 10, "once": 1, "twice": 2, "single": 1, "both": 2, "pair": 2, "triple": 3}
_EN_MULT = {"thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9, "trillion": 10 ** 12}
_JA_MULT = {"百": 10 ** 2, "千": 10 ** 3, "万": 10 ** 4, "億": 10 ** 8, "兆": 10 ** 12}
_MONTHS = {m: i + 1 for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                           "october", "november", "december"])}
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")

_NUM = re.compile(
    r"(?P<num>\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
    r"(?:\s*(?:[×x×]\s*10\s*\^?\s*(?P<e1>[-−–+]?\d+)|[eE](?P<e2>[-−+]?\d+)))?"
    r"(?:\s*(?P<en>thousand|million|billion|trillion)\b|(?P<ja>[百千万億兆]))?", re.I)
_OPS_BEFORE = re.compile(r"(?:^|[^A-Za-z0-9])(?:[A-Za-zα-ωΑ-Ω_]{1,3}(?:<sub>[^<]*</sub>)?\s*)?(<=|>=|≤|≥|⩽|⩾|≠|<|>|=|≈|~)\s*$")
_OPS_AFTER = re.compile(r"^\s*(?:%|[°℃]|[A-Za-z]{0,3})?\s*(未満|以下|超|以上|より大き|より高|より多|を超え|を上回|より小さ|より低|より少な|を下回|を下まわ)")
_OPS_NEGATED_AFTER = re.compile(
    r"^\s*(?:%|[°℃]|[A-Za-z]{0,3})?\s*(?:"
    r"(未満|以下|超|以上)\s*(?:で(?:は)?|じゃ)\s*(?:ない|なかった|ありません)"
    r"|(より大き|より高|より多|より小さ|より低|より少な)\s*く\s*(?:は\s*)?(?:ない|なかった|ありません)"
    r"|(を超え)\s*(?:ない|なかった|ません)"
    r"|(を上回|を下回|を下まわ)\s*(?:ら(?:ない|なかった)|りません))")
_OP_NORM = {"≤": "<=", "≥": ">=", "⩽": "<=", "⩾": ">=", "未満": "<", "以下": "<=", "超": ">", "以上": ">=", "~": "~", "≈": "~",
            # 言い換え (0.05 より大きい / 0.05 を下回る)。向きが逆の言い換え (> を「より小さい」など) は演算子の不一致として拒否する
            "より大き": ">", "より高": ">", "より多": ">", "を超え": ">", "を上回": ">",
            "より小さ": "<", "より低": "<", "より少な": "<", "を下回": "<", "を下まわ": "<"}
_OP_NEGATE = {">": "<=", "<": ">=", ">=": "<", "<=": ">"}
_FAMILY = {">": ">", ">=": ">", "<": "<", "<=": "<"}      # 向きが同じ比較 (> と >=、< と <=) の言い換えは許す (「以上」「超」など)。向きが逆なら拒否
_STRICT = {"<", ">", "<=", ">=", "≠"}   # 意味が変わる比較演算子 (= や ~ は、片方に無いだけなら問題にしない)
_COUNTERS = re.compile(r"^(つ|個|件|点|回|人|名|頭|匹|本|枚|種|段階|番目|章|節|列|行|桁|倍|か所|ヶ所|カ所|次元|群|組|日|週|か月|ヵ月|ヶ月|年|時間|分|秒|日間|週間|年間|世紀|第)")
_HTML_TAG = re.compile(
    r"""</?[A-Za-z][A-Za-z0-9:-]*"""
    r"""(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*"""
    r"""\s*/?>""")


def _plain(s: str) -> str:
    """タグ・{vN}・⟦n⟧ を外し (上付きは ^ で残す)、HTML 実体を戻し、NFKC にする。引用 [n] は外す。"""
    s = re.sub(r"<sup>\s*([^<]*)</sup>", lambda m: "^" + m.group(1).translate(_SUP), s)
    # タグ名と属性の構文を要求し、「p < 0.05; q > 0.01」をタグとして消さない。
    s = _HTML_TAG.sub("", s)
    s = re.sub(r"⟦\d+⟧|\{v\d+\}", " ", s)
    s = htmllib.unescape(s)
    s = unicodedata.normalize("NFKC", s).replace("−", "-").replace("–", "-").replace("‐", "-")
    s = re.sub(r"\[\s*\d+(?:\s*[,\-]\s*\d+)*\s*\]", " ", s)
    # 数量の書き方をそろえる: 「50k」「10k」= 50 thousand / 10 thousand、「10-50 thousand」「1-2 million」= 10 thousand-50 thousand など
    # (訳文の「5万」「1万〜5万」と同じ値に換算できるように。両側に同じ変換をかけるので、原文と訳文の書き方の違いだけを吸収する)
    s = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s*[kK](?![A-Za-z])", r"\1 thousand", s)
    # 「15.3/19.6 billion」「10-50 thousand」のように、並べた数の最後にだけ付いた単位は、前の数にも付ける
    s = re.sub(r"(\d[\d,]*(?:\.\d+)?)\s*([-/])\s*(\d[\d,]*(?:\.\d+)?)\s*(thousand|million|billion|trillion)\b",
               r"\1 \4\2\3 \4", s, flags=re.I)
    return s


def _dec(num: str) -> Decimal:
    return Decimal(num.replace(",", ""))


def _norm(d: Decimal) -> str:
    d = d.normalize() if d != 0 else Decimal(0)
    return format(d, "f") if abs(d.adjusted()) < 30 else str(d)


def tokens(text: str) -> list[tuple[str, str, str]]:
    """(演算子, 符号, 値) のリスト。text は _plain 済み。"""
    out = []
    for m in _NUM.finditer(text):
        a, b = m.start(), m.end()
        before = text[:a]
        try:
            v = _dec(m.group("num"))
            e = m.group("e1") or m.group("e2")
            if e:
                v = v * (Decimal(10) ** int(e.replace("−", "-").replace("+", "")))
            mult = _EN_MULT.get((m.group("en") or "").lower()) or _JA_MULT.get(m.group("ja") or "")
            if mult:
                v = v * mult
        except (InvalidOperation, ValueError):
            continue
        sign = ""
        k = a - 1
        while k >= 0 and text[k] == " ":
            k -= 1
        if k >= 0 and text[k] == "-" and (k == 0 or not (text[k - 1].isalnum() and text[k - 1].isascii())):
            sign = "-"
            before = text[:k]
        elif k >= 0 and text[k] == "+" and (k == 0 or not text[k - 1].isalnum()):
            before = text[:k]
        after = text[b:]
        op = ""
        om = _OPS_BEFORE.search(before)
        if om:
            op = _OP_NORM.get(om.group(1), om.group(1))
        else:
            # 語幹だけの肯定形より先に否定形を照合する。等号の有無も反転する。
            nm = _OPS_NEGATED_AFTER.match(after)
            if nm:
                stem = next(g for g in nm.groups() if g is not None)
                op = _OP_NEGATE[_OP_NORM[stem]]
            else:
                am = _OPS_AFTER.match(after)
                if am:
                    op = _OP_NORM.get(am.group(1), am.group(1))
        out.append((op, sign, _norm(v)))
    return out


def _words(src_plain: str) -> set[str]:
    """原文中の数詞 (three, twenty ...) の値。訳文が算用数字にしてよい数。"""
    s = set()
    ws = re.findall(r"[A-Za-z]+", src_plain.lower())
    for i, w in enumerate(ws):
        v = _WORDS.get(w)
        if v is not None:
            s.add(str(v))
            if i + 1 < len(ws) and ws[i + 1].startswith("decade"):
                s.add(str(v * 10))          # two decades -> 20 年
        if v is not None and v in (20, 30, 40, 50, 60, 70, 80, 90) and i + 1 < len(ws) and _WORDS.get(ws[i + 1]) in range(1, 10) \
                and ws[i + 1] not in ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"):
            s.add(str(v + _WORDS[ws[i + 1]]))   # thirty-six -> 36 (ハイフン・空白で区切った複合数詞)
        if w in _MONTHS:
            s.add(str(_MONTHS[w]))           # June -> 6月
        if w.startswith("centur"):
            s.update({"1", "100"})           # a century -> 1 世紀 / 100 年
    return s


def check_numbers(src: str, ja: str, ocr: bool = False) -> list[str]:
    """原文と訳文の数値表現を照合し、問題の説明のリストを返す (空なら OK)。"""
    sp, jp = _plain(src), _plain(ja)
    st, jt = tokens(sp), tokens(jp)
    problems: list[str] = []
    jc = Counter(jt)
    jvals = Counter((s, v) for _o, s, v in jt)
    missing = []
    for tok in st:
        if jc[tok] > 0:
            jc[tok] -= 1
            continue
        op, sign, v = tok
        # 演算子の表記違い (原文は演算子があり訳文は無い/逆) は、値と符号が一致すれば演算子だけの変化として別に扱う
        alt = next((t for t in jc if jc[t] > 0 and t[1] == sign and t[2] == v), None)
        if alt is not None:
            jc[alt] -= 1
            if op != alt[0] and op in _STRICT:   # 原文に < > ≤ ≥ があるのに訳文で変わった/消えた (原文に演算子が無く「未満」などに言い換えた場合は問題にしない)
                problems.append(f"比較演算子が原文と違う: 「{op or '(なし)'}{sign}{v}」→「{alt[0] or '(なし)'}{alt[1]}{alt[2]}」")
            continue
        # 符号だけ違う
        alt = next((t for t in jc if jc[t] > 0 and t[2] == v and t[1] != sign), None)
        if alt is not None:
            jc[alt] -= 1
            problems.append(f"符号が原文と違う: 「{sign}{v}」→「{alt[1]}{alt[2]}」")
            continue
        missing.append(f"{sign}{v}")
    # 余分な数値 (原文に無い数値を訳文が足していないか)
    src_vals = {v for _o, _s, v in st}
    words = _words(sp)
    mult_vals = {str(m) for w, m in _EN_MULT.items() if re.search(r"\b" + w + r"\b", sp, re.I)}   # a million -> 1000000 のように数字で書き直した数量
    if re.search(r"\bhundred\b", sp, re.I):
        mult_vals.add("100")
    extras = []
    for (op, sign, v), n in jc.items():
        if n <= 0 or v in src_vals or v in words or v in mult_vals:
            continue
        try:
            dv = Decimal(v)
        except InvalidOperation:
            continue
        small_int = dv == dv.to_integral_value() and 0 <= dv <= 10
        if small_int:
            # 1〜10 の整数は、数詞の言い換え・個数表現 (2つ など) の可能性が高いので、和の助数詞が付くときだけ許す
            if any(re.match(_COUNTERS, jp[m.end():]) or re.search(r"[A-Za-z]\s?$", jp[:m.start()])
                   for m in re.finditer(r"(?<![\d.])" + re.escape(v) + r"(?![\d.])", jp)):
                continue          # 助数詞つき (2つ)、または識別子の一部 (Dreem 2) は許す
        extras.append(f"{sign}{v}")
    if ocr and missing and extras:
        # OCR が小数点を落とした数 (09 % -> 0.9 %) を訳文が直したときは、欠落/余分の組を相殺する
        def _digits(x: str) -> str:
            return x.lstrip("+-\u2212").replace(".", "").replace(",", "").lstrip("0")
        for m in list(missing):
            for e in list(extras):
                if "." not in m and "." in e and _digits(m) and _digits(m) == _digits(e):
                    missing.remove(m)
                    extras.remove(e)
                    break
    if missing:
        problems.append("数値が欠落/変化: " + ",".join(sorted(set(missing))[:6]))
    if extras:
        problems.append("訳文に原文に無い数値がある: " + ",".join(sorted(set(extras))[:6]))
    return problems
