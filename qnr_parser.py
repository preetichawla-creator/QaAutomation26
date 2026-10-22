"""
Word questionnaire (QNR) -> structured question list.

Two modes:
  * Claude API (recommended): reads the whole QNR text and returns JSON.
  * Heuristic fallback: regex-based, works offline for cleanly formatted QNRs.

Either way the output is the same JSON structure, which the tester can review
and edit in the app before the live link is tested.

Question schema
---------------
{
  "id": "Q1",                       # question name as in the QNR
  "text": "What is your age?",      # respondent-facing text only
  "type": "single|multi|numeric|open|grid|info",
  "options": [{"code": "1", "label": "Male", "terminate": false, "exclusive": false}],
  "rows":    [{"code": "1", "label": "Brand A"}],      # grid rows (grid only)
  "validation": {"required": true, "min": null, "max": null,
                 "min_select": null, "max_select": null},
  "randomize": false,
  "answer": null,                   # optional: what the bot should answer
  "notes": ""                       # programmer instructions, routing text
}
"""
from __future__ import annotations

import json
import re
from typing import Iterator

import docx
from docx.document import Document as _Doc
from docx.table import Table
from docx.text.paragraph import Paragraph

DEFAULT_MODEL = "claude-sonnet-5-5"


# --------------------------------------------------------------------------
# Reading the .docx in document order (paragraphs and tables interleaved)
# --------------------------------------------------------------------------
def _iter_blocks(document: _Doc) -> Iterator[Paragraph | Table]:
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, document)
        elif tag == "tbl":
            yield Table(child, document)


def _para_text(p: Paragraph) -> str:
    text = p.text.strip()
    # Keep Word auto-numbering visible as a hint (python-docx drops the number)
    if text and p._p.pPr is not None and p._p.pPr.numPr is not None:
        text = "• " + text
    return text


def docx_to_lines(path) -> list[dict]:
    """Return [{'kind': 'p'|'row', 'text': str, 'cells': [..]}] in order."""
    document = docx.Document(path)
    lines: list[dict] = []
    for block in _iter_blocks(document):
        if isinstance(block, Paragraph):
            t = _para_text(block)
            if t:
                lines.append({"kind": "p", "text": t, "cells": None})
        else:
            for row in block.rows:
                cells = []
                for c in row.cells:
                    ct = " ".join(x.text.strip() for x in c.paragraphs if x.text.strip())
                    if not cells or cells[-1] != ct:  # merged cells repeat
                        cells.append(ct)
                if any(cells):
                    lines.append({"kind": "row", "text": " | ".join(cells), "cells": cells})
    return lines


def docx_to_text(path) -> str:
    out = []
    for ln in docx_to_lines(path):
        out.append(("TABLE ROW: " if ln["kind"] == "row" else "") + ln["text"])
    return "\n".join(out)


# --------------------------------------------------------------------------
# Normalisation of a parsed list (shared by both modes)
# --------------------------------------------------------------------------
def _num(v):
    if v in (None, ""):
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return None


def normalise(questions: list[dict]) -> list[dict]:
    clean = []
    for q in questions:
        qtype = (q.get("type") or "single").lower()
        if qtype not in {"single", "multi", "numeric", "open", "grid", "info"}:
            qtype = "single"
        val = q.get("validation") or {}
        opts = []
        for o in q.get("options") or []:
            opts.append({
                "code": str(o.get("code", "")).strip(),
                "label": str(o.get("label", "")).strip(),
                "terminate": bool(o.get("terminate", False)),
                "exclusive": bool(o.get("exclusive", False)),
            })
        rows = [{"code": str(r.get("code", "")).strip(), "label": str(r.get("label", "")).strip()}
                for r in (q.get("rows") or [])]
        clean.append({
            "id": str(q.get("id", "")).strip(),
            "text": str(q.get("text", "")).strip(),
            "type": qtype,
            "options": opts,
            "rows": rows,
            "validation": {
                "required": bool(val.get("required", qtype != "info")),
                "min": _num(val.get("min")),
                "max": _num(val.get("max")),
                "min_select": _num(val.get("min_select")),
                "max_select": _num(val.get("max_select")),
            },
            "randomize": bool(q.get("randomize", False)),
            "answer": q.get("answer"),
            "notes": str(q.get("notes", "") or "").strip(),
        })
    return [q for q in clean if q["id"]]


# --------------------------------------------------------------------------
# Mode 1: Claude API
# --------------------------------------------------------------------------
CLAUDE_PROMPT = """You are a market-research survey QA specialist. Below is the text of a Word \
questionnaire (QNR). Table rows are prefixed with "TABLE ROW:" and cells are separated by " | ".

Extract EVERY respondent-facing question in order and return ONLY a JSON array (no prose, no \
markdown fences). Each element:

{
  "id": question name exactly as in the QNR (e.g. "S1", "Q3a", "QM1.1"),
  "text": the respondent-facing question text only. Remove programmer instructions such as \
[SINGLE CODE], [ASK ALL], (SHOW CARD), routing, base definitions and scripter notes. Keep \
respondent-facing instructions like "Select all that apply" if they appear as part of the question.
  "type": one of "single", "multi", "numeric", "open", "grid", "info" (info = text screen, no answer),
  "options": [{"code": "1", "label": "...", "terminate": true/false, "exclusive": true/false}] \
- answer list with codes exactly as in the QNR; terminate=true if the option is marked \
TERMINATE/TERM/CLOSE/SCREEN OUT; exclusive=true if marked EXCLUSIVE or it is a "None of these"/\
"Don't know" option flagged as single/exclusive in a multi question. Strip those flags from the label. \
For grids, options are the columns/scale points.
  "rows": for grid questions only, the row statements [{"code": "1", "label": "..."}], else [],
  "validation": {"required": true unless marked optional, "min": numeric minimum or null, \
"max": numeric maximum or null, "min_select": minimum selections for multi or null, \
"max_select": maximum selections for multi or null},
  "randomize": true if options/rows are marked to randomise/rotate,
  "answer": null,
  "notes": short summary of programmer instructions and routing (ASK IF, SKIP TO, piping, base)
}

If something is ambiguous, make your best reading and mention the ambiguity in "notes".

QUESTIONNAIRE:
"""


def _extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("Claude did not return a JSON array")
    return json.loads(text[start:end + 1])


def _chunks(text: str, limit: int = 45000) -> list[str]:
    """Split long QNRs on line boundaries, preferring lines that start a question."""
    if len(text) <= limit:
        return [text]
    lines, parts, cur = text.split("\n"), [], []
    size = 0
    for ln in lines:
        starts_q = bool(QUESTION_RE.match(ln))
        if size > limit * 0.8 and starts_q or size > limit:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def parse_with_claude(path, api_key: str, model: str = DEFAULT_MODEL, log=print) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    text = docx_to_text(path)
    parts = _chunks(text)
    questions: list[dict] = []
    for i, part in enumerate(parts, 1):
        log(f"Sending QNR part {i}/{len(parts)} to Claude ({len(part):,} chars)...")
        with client.messages.stream(
            model=model,
            max_tokens=32000,
            messages=[{"role": "user", "content": CLAUDE_PROMPT + part}],
        ) as stream:
            msg = stream.get_final_message()
        reply = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        questions.extend(_extract_json_array(reply))
    # de-duplicate ids that straddle chunk boundaries (keep the fuller one)
    seen: dict[str, dict] = {}
    for q in questions:
        k = str(q.get("id", "")).lower()
        if k not in seen or len(q.get("options") or []) > len(seen[k].get("options") or []):
            seen[k] = q
    ordered = []
    for q in questions:
        k = str(q.get("id", "")).lower()
        if seen.get(k) is q:
            ordered.append(q)
    return normalise(ordered)


# --------------------------------------------------------------------------
# Mode 2: heuristic parser
# --------------------------------------------------------------------------
QUESTION_RE = re.compile(
    r"^\s*(?:•\s*)?([A-Z]{1,5}\d{1,3}[a-zA-Z]?(?:[._]\d{1,3}[a-zA-Z]?)*)\s*[.):\-]?\s+(\S.*)$"
)
OPTION_CODE_FIRST = re.compile(r"^\s*(?:•\s*)?\(?(\d{1,3})\)?\s*[.):\-]?\s+(\S.*)$")
OPTION_CODE_LAST = re.compile(r"^\s*(?:•\s*)?(\S.*?)[\s.]{2,}(\d{1,3})\s*$")
SCALE_LINE = re.compile(r"^\s*(?:columns?|scale|grid columns?)\s*[:\-]\s*(.+)$", re.I)
SCALE_ITEM = re.compile(r"(\d{1,3})\s*[.)=:\-]\s*([^0-9]+?)(?=\s+\d{1,3}\s*[.)=:\-]|$)")
BRACKETS = re.compile(r"[\[{(]([^\]})]{2,120})[\]})]")

TYPE_HINTS = [
    ("grid", r"\bGRID\b|\bMATRIX\b|\bSINGLE CODE PER ROW\b"),
    ("multi", r"\bMULTI|\bMA\b|\bSELECT ALL\b|\bCODE ALL\b|\bMULTIPLE\b|\bALL THAT APPLY\b"),
    ("numeric", r"\bNUMERIC\b|\bNUMBER\b|\bNUM\b|\bRANGE\b"),
    ("open", r"\bOPEN\b|\bOE\b|\bTEXT ?BOX\b|\bVERBATIM\b|\bSPECIFY\b(?!.*CODE)"),
    ("info", r"\bINFO\b|\bTEXT SCREEN\b|\bINTRO\b|\bNO RESPONSE\b"),
    ("single", r"\bSINGLE|\bSA\b|\bSELECT ONE\b|\bONE ANSWER\b"),
]
TERM_RE = re.compile(r"[\[(]?\s*\b(TERMINATE|TERM|CLOSE|SCREEN ?OUT|THANK AND END)\b\s*[\])]?", re.I)
EXCL_RE = re.compile(r"[\[(]?\s*\b(EXCLUSIVE|EXCL|SINGLE CODE ONLY|ANCHOR(?:ED)?|FIX(?:ED)?)\b\s*[\])]?", re.I)
RANGE_RE = re.compile(r"(?:RANGE|ALLOW|BETWEEN)?\s*(-?\d+(?:\.\d+)?)\s*(?:-|–|TO)\s*(-?\d+(?:\.\d+)?)", re.I)
MAXSEL_RE = re.compile(r"(?:UP TO|MAX(?:IMUM)?(?: OF)?|NO MORE THAN)\s*(\d+)", re.I)
MINSEL_RE = re.compile(r"(?:AT LEAST|MIN(?:IMUM)?(?: OF)?)\s*(\d+)", re.I)


def _instructions(text: str) -> list[str]:
    """Bracketed bits that look like programmer instructions (mostly upper case)."""
    out = []
    for m in BRACKETS.finditer(text):
        inner = m.group(1)
        letters = [c for c in inner if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            out.append(inner)
    return out


def _strip_instructions(text: str) -> str:
    def repl(m):
        inner = m.group(1)
        letters = [c for c in inner if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
            return " "
        return m.group(0)
    return re.sub(r"\s+", " ", BRACKETS.sub(repl, text)).strip()


def _apply_hints(q: dict, instr: str):
    for qtype, pat in TYPE_HINTS:
        if re.search(pat, instr, re.I):
            q["type"] = qtype
            break
    if re.search(r"\bRANDOMI[SZ]E|\bROTATE\b", instr, re.I):
        q["randomize"] = True
    if re.search(r"\bOPTIONAL\b|\bNOT MANDATORY\b", instr, re.I):
        q["validation"]["required"] = False
    m = MAXSEL_RE.search(instr)
    if m:
        q["validation"]["max_select"] = int(m.group(1))
    m = MINSEL_RE.search(instr)
    if m:
        q["validation"]["min_select"] = int(m.group(1))
    m = RANGE_RE.search(instr)
    if m and (q["type"] == "numeric" or re.search(r"RANGE|NUMERIC|NUMBER|ALLOW", instr, re.I)):
        q["validation"]["min"], q["validation"]["max"] = _num(m.group(1)), _num(m.group(2))
        if q["type"] not in ("numeric",):
            q["type"] = "numeric"


def _make_option(code: str, label: str) -> dict:
    term = bool(TERM_RE.search(label))
    excl = bool(EXCL_RE.search(label))
    label = TERM_RE.sub(" ", label)
    label = EXCL_RE.sub(" ", label)
    label = _strip_instructions(label)
    label = re.sub(r"[\s\-–→>]+$", "", label).strip()
    return {"code": code, "label": label, "terminate": term, "exclusive": excl}


def parse_heuristic(path) -> list[dict]:
    lines = docx_to_lines(path)
    questions: list[dict] = []
    cur: dict | None = None

    def new_q(qid, rest):
        q = {"id": qid, "text": "", "type": "single", "options": [], "rows": [],
             "validation": {"required": True, "min": None, "max": None,
                            "min_select": None, "max_select": None},
             "randomize": False, "answer": None, "notes": ""}
        instr = " ".join(_instructions(rest))
        q["notes"] = instr
        q["text"] = _strip_instructions(rest)
        _apply_hints(q, instr)
        return q

    for ln in lines:
        text = ln["text"]
        if ln["kind"] == "p":
            m = QUESTION_RE.match(text)
            if m:
                cur = new_q(m.group(1), m.group(2))
                questions.append(cur)
                continue
            if cur is None:
                continue
            ms = SCALE_LINE.match(text)
            if ms:  # "Columns: 1. Poor 2. Fair 3. Good" / "Scale: 1 = Poor, 5 = Good"
                for code, label in SCALE_ITEM.findall(ms.group(1)):
                    cur["options"].append(_make_option(code, label.strip(" ,;")))
                if cur["type"] == "single":
                    cur["type"] = "grid"
                continue
            mo = OPTION_CODE_FIRST.match(text) or None
            ml = OPTION_CODE_LAST.match(text) if not mo else None
            if mo:
                cur["options"].append(_make_option(mo.group(1), mo.group(2)))
            elif ml:
                cur["options"].append(_make_option(ml.group(2), ml.group(1)))
            else:
                instr = _instructions(text)
                stripped = _strip_instructions(text)
                if instr:
                    cur["notes"] = (cur["notes"] + " " + " ".join(instr)).strip()
                    _apply_hints(cur, " ".join(instr))
                if stripped and not cur["options"]:
                    # continuation of the question text (before options start)
                    if len(stripped) > 3 and not stripped.isupper():
                        cur["text"] = (cur["text"] + " " + stripped).strip()
        else:  # table row
            if cur is None:
                continue
            cells = [c for c in ln["cells"] if c]
            if not cells:
                continue
            code_cells = [c for c in cells if re.fullmatch(r"\(?\d{1,3}\)?\.?", c.strip())]
            label_cells = [c for c in cells if c not in code_cells]
            if code_cells and label_cells:
                code = re.sub(r"\D", "", code_cells[0])
                label = " ".join(label_cells)
                target = "rows" if cur["type"] == "grid" else "options"
                if target == "rows":
                    cur["rows"].append({"code": code, "label": _strip_instructions(label)})
                else:
                    cur["options"].append(_make_option(code, label))
            elif cur["type"] == "grid" and label_cells and not code_cells:
                cur["rows"].append({"code": str(len(cur["rows"]) + 1),
                                    "label": _strip_instructions(" ".join(label_cells))})

    for q in questions:
        if q["type"] == "single" and not q["options"]:
            q["type"] = "info" if not q["text"].rstrip().endswith("?") else "open"
        if q["type"] == "info":
            q["validation"]["required"] = False
    return normalise(questions)


def parse_qnr(path, api_key: str | None = None, model: str = DEFAULT_MODEL, log=print) -> list[dict]:
    if api_key:
        try:
            return parse_with_claude(path, api_key, model, log)
        except Exception as e:  # fall back rather than stop the tester
            log(f"Claude parsing failed ({e}); falling back to heuristic parser.")
    return parse_heuristic(path)


if __name__ == "__main__":
    import os
    import sys

    qs = parse_qnr(sys.argv[1], os.getenv("ANTHROPIC_API_KEY"))
    print(json.dumps(qs, indent=2, ensure_ascii=False))
