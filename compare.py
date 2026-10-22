"""Text / option / type comparison between a QNR question and what the live page shows."""
from __future__ import annotations

import difflib
import html
import re
import unicodedata

from rapidfuzz import fuzz

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"

_QUOTES = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
           "—": "-", "…": "...", " ": " "}


def norm(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    for a, b in _QUOTES.items():
        text = text.replace(a, b)
    # drop programmer instructions in brackets that are mostly upper case
    def repl(m):
        inner = m.group(1)
        letters = [c for c in inner if c.isalpha()]
        return " " if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6 else m.group(0)
    text = re.sub(r"[\[{]([^\]}]{1,150})[\]}]", repl, text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    text = re.sub(r"\s*([?.,:;!])", r"\1", text)
    return text


def similarity(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    if not a and not b:
        return 100.0
    return round(fuzz.ratio(a, b), 1)


def word_diff(expected: str, actual: str) -> str:
    e, a = norm(expected).split(), norm(actual).split()
    missing, extra = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, e, a).get_opcodes():
        if tag in ("delete", "replace"):
            missing.append(" ".join(e[i1:i2]))
        if tag in ("insert", "replace"):
            extra.append(" ".join(a[j1:j2]))
    parts = []
    if missing:
        parts.append("QNR has, link lacks: " + " | ".join(missing))
    if extra:
        parts.append("Link has, QNR lacks: " + " | ".join(extra))
    return "; ".join(parts)


def _status(score: float, pass_at: float, warn_at: float) -> str:
    return PASS if score >= pass_at else WARN if score >= warn_at else FAIL


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9À-￿]+(?:'[a-z]+)?", norm(text))


def text_status(expected: str, actual: str, strict: bool = True,
                pass_at: float = 95, warn_at: float = 80) -> tuple[str, float, str]:
    """Strict mode: identical words -> PASS, punctuation/case only -> WARN, any word change -> FAIL."""
    score = similarity(expected, actual)
    if not strict:
        st = _status(score, pass_at, warn_at)
        return st, score, "" if st == PASS else word_diff(expected, actual)
    if norm(expected) == norm(actual):
        return PASS, score, ""
    if _words(expected) == _words(actual):
        return WARN, score, "Only punctuation / spacing differs"
    return FAIL, score, word_diff(expected, actual)


def _contains_words(expected: str, actual: str) -> bool:
    e, a = " ".join(_words(expected)), " ".join(_words(actual))
    return bool(e) and e in a


def compare_question(q: dict, live: dict, pass_at: float = 95, warn_at: float = 80,
                     strict: bool = True) -> list[dict]:
    """Return a list of finding rows for one matched question."""
    rows = []

    def add(check, expected, actual, status, score=None, note=""):
        rows.append({"question": q["id"], "check": check, "qnr": expected, "live": actual,
                     "similarity": score, "status": status, "note": note})

    # ---- question text --------------------------------------------------
    if q.get("text"):
        live_text = live.get("text", "")
        st, score, note = text_status(q["text"], live_text, strict, pass_at, warn_at)
        if st != PASS and _contains_words(q["text"], live_text):
            st, note = INFO, "QNR text is shown exactly, plus extra text on the link: " + \
                word_diff(q["text"], live_text).replace("Link has, QNR lacks: ", "")
        add("Question text", q["text"], live_text, st, score, note)

    # ---- type -------------------------------------------------------------
    expected_kind = {"single": "radio", "multi": "checkbox", "numeric": "text",
                     "open": "text", "grid": "grid"}.get(q["type"])
    lk = live.get("kind")
    if expected_kind and lk:
        ok = (expected_kind == lk or (expected_kind == "radio" and lk == "select")
              or (expected_kind == "grid" and lk in ("grid", "radio", "checkbox"))
              or (q["type"] == "multi" and lk == "grid"))
        add("Question type", q["type"], lk, PASS if ok else FAIL, None,
            "" if ok else f"QNR says {q['type']} but the page renders {lk} inputs")

    # ---- options ----------------------------------------------------------
    if q["type"] in ("single", "multi", "grid") and q.get("options"):
        rows.extend(compare_options(q, live, pass_at, warn_at, strict))

    # ---- grid rows --------------------------------------------------------
    if q["type"] == "grid" and q.get("rows"):
        row_labels = live.get("row_labels", [])
        used = set()
        for r in q["rows"]:
            cands = [(similarity(r["label"], x), i, x) for i, x in enumerate(row_labels) if i not in used]
            if not cands:
                add(f"Grid row {r['code']}", r["label"], "MISSING", FAIL, None, "Row not shown on the link")
                continue
            s, i, x = max(cands)
            if s < warn_at - 20:
                add(f"Grid row {r['code']}", r["label"], "MISSING", FAIL, None, "Row not shown on the link")
                continue
            used.add(i)
            st, score, note = text_status(r["label"], x, strict, pass_at, warn_at)
            add(f"Grid row {r['code']}", r["label"], x, st, score, note)
        for i, x in enumerate(row_labels):
            if i not in used and len(row_labels) > len(q["rows"]):
                add("Extra grid row", "", x, FAIL, None, "Row shown on link but not in QNR")
    return rows


def compare_options(q: dict, live: dict, pass_at: float, warn_at: float, strict: bool = True) -> list[dict]:
    rows = []
    qopts = q["options"]
    lopts = live.get("options", [])

    def add(check, expected, actual, status, score=None, note=""):
        rows.append({"question": q["id"], "check": check, "qnr": expected, "live": actual,
                     "similarity": score, "status": status, "note": note})

    if not lopts:
        add("Options", f"{len(qopts)} options", "no options detected", FAIL, None,
            "No answer options were found on the page for this question")
        return rows

    live_codes = [o.get("code") for o in lopts]
    codes_usable = all(c not in (None, "", "on") for c in live_codes) and len(set(live_codes)) == len(live_codes)
    used_live = set()
    order_qnr, order_live = [], []

    for qo in qopts:
        match, how = None, ""
        if codes_usable:
            for i, lo in enumerate(lopts):
                if i not in used_live and str(lo.get("code")) == str(qo["code"]):
                    match, how = i, "code"
                    break
        if match is None:  # label based
            best_i, best_s = None, 0
            for i, lo in enumerate(lopts):
                if i in used_live:
                    continue
                s = similarity(qo["label"], lo.get("label", ""))
                if s > best_s:
                    best_i, best_s = i, s
            if best_i is not None and best_s >= warn_at:
                match, how = best_i, "label"
        if match is None:
            add(f"Option {qo['code']}", qo["label"], "MISSING", FAIL, None,
                "Option in QNR not shown on the link" + (" (check if it is a piped/conditional list)" if q.get("notes") else ""))
            continue
        used_live.add(match)
        lo = lopts[match]
        st, s, note = text_status(qo["label"], lo.get("label", ""), strict, pass_at, warn_at)
        if how == "label" and codes_usable and str(lo.get("code")) != str(qo["code"]):
            st = FAIL
            note = f"Label matches but code differs: QNR {qo['code']} vs link {lo.get('code')}"
        add(f"Option {qo['code']}", qo["label"], f"[{lo.get('code')}] {lo.get('label', '')}", st, s, note)
        order_qnr.append(qo["code"])
        order_live.append((match, qo["code"]))

    for i, lo in enumerate(lopts):
        if i not in used_live:
            add(f"Extra option {lo.get('code')}", "", lo.get("label", ""), FAIL, None,
                "Option shown on link but not in QNR")

    live_seq = [c for _, c in sorted(order_live)]
    if live_seq and live_seq != order_qnr:
        st = INFO if q.get("randomize") else WARN
        add("Option order", " ".join(order_qnr), " ".join(live_seq), st, None,
            "Order differs (expected, QNR asks to randomise)" if q.get("randomize")
            else "Order differs from QNR (not marked randomised)")
    return rows
