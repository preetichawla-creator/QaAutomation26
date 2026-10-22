"""
Playwright bot that walks a Sawtooth Lighthouse Studio live link page by page,
compares every question with the parsed QNR, runs negative validation tests,
then answers validly and moves on.

Page selectors live in SELECTORS so they can be adjusted for custom templates.
"""
from __future__ import annotations

import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from compare import FAIL, INFO, PASS, WARN, compare_question, norm
from rapidfuzz import fuzz

SELECTORS = {
    # Lighthouse renders each question as <div id="Q1_div" class="question select">
    "container": "div.question[id$='_div']",
    "container_fallback": "div[id$='_div']",
    "question_text": [".header1", ".header2", ".question_text"],
    "next": ["#next_button", "button.next_button", "input.next_button",
             "input[name='next_button']", "button:has-text('Next')",
             "input[type='submit'][value*='Next' i]", "input[type='button'][value*='Next' i]"],
    "errors": [".error_messages", ".question_error", ".error", "[class*='error' i]", "[id*='error' i]"],
}

END_COMPLETE = re.compile(r"thank you|survey is (now )?complete|completed the survey|your responses have been", re.I)
END_TERM = re.compile(r"do not qualify|not qualify|screened out|unfortunately|not eligible|quota (is )?full|terminat", re.I)
AVOID_LABEL = re.compile(r"\bother\b|specify|none of|don.?t know|not sure|prefer not|refuse|n/?a\b", re.I)


@dataclass
class Settings:
    headless: bool = True
    run_validations: bool = True
    max_pages: int = 150
    pass_at: float = 95
    warn_at: float = 80
    slow_mo: int = 0
    nav_timeout: float = 6.0       # seconds to wait for a page to advance
    viewport: tuple = (1366, 900)
    strict: bool = True            # any wording change in text/options = FAIL
    replay_on_fail: bool = True    # restart and walk back to finish a page's remaining tests


@dataclass
class RunResult:
    findings: list = field(default_factory=list)      # text/option/type checks
    validations: list = field(default_factory=list)   # negative tests
    path: list = field(default_factory=list)          # page log
    seen: dict = field(default_factory=dict)          # qnr id -> page no
    end_state: str = "Not started"
    end_text: str = ""
    errors: list = field(default_factory=list)


# --------------------------------------------------------------------------
# JavaScript run inside the page
# --------------------------------------------------------------------------
EXTRACT_JS = r"""
(cfg) => {
  const vis = el => { if (!el) return false; const r = el.getClientRects();
    const s = getComputedStyle(el); return r.length && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  let conts = Array.from(document.querySelectorAll(cfg.container));
  if (!conts.length) conts = Array.from(document.querySelectorAll(cfg.container_fallback))
                        .filter(d => d.querySelector('input,select,textarea') || d.querySelector(cfg.textSel));
  conts = conts.filter(c => !conts.some(o => o !== c && o.contains(c))).filter(vis);
  let counter = 0;
  const colHeader = (inp) => {
    const td = inp.closest('td,th'); if (!td) return '';
    const tr = td.parentElement, table = tr.closest('table'); if (!table) return '';
    const idx = Array.from(tr.children).indexOf(td);
    const rows = Array.from(table.querySelectorAll('tr'));
    let header = '';
    for (const r of rows) { if (r.querySelector('input,select,textarea')) break;
      const cell = r.children[idx]; if (cell && clean(cell.innerText)) header = clean(cell.innerText); }
    return header;
  };
  const rowLabel = (inp) => {
    const tr = inp.closest('tr'); if (!tr) return '';
    for (const cell of tr.children) { if (!cell.querySelector('input,select,textarea') && clean(cell.innerText))
      return clean(cell.innerText); }
    return '';
  };
  const labelOf = (inp, c) => {
    if (inp.id) { const l = c.querySelector(`label[for="${CSS.escape(inp.id)}"]`) || document.querySelector(`label[for="${CSS.escape(inp.id)}"]`);
      if (l && clean(l.innerText)) return clean(l.innerText); }
    const pl = inp.closest('label'); if (pl && clean(pl.innerText)) return clean(pl.innerText);
    const td = inp.closest('td');
    if (td) { if (clean(td.innerText)) return clean(td.innerText);
      const nxt = td.nextElementSibling; if (nxt && !nxt.querySelector('input') && clean(nxt.innerText)) return clean(nxt.innerText); }
    const li = inp.closest('li,.option,.response'); if (li && clean(li.innerText)) return clean(li.innerText);
    let s = inp.nextSibling; let t = '';
    while (s && t.length < 200) { if (s.nodeType === 1 && s.matches('input,br')) break; t += ' ' + (s.textContent || ''); s = s.nextSibling; }
    return clean(t);
  };
  const out = conts.map(c => {
    const name = c.id.replace(/_div$/, '');
    const headers = cfg.textSel.split(',').flatMap(s => Array.from(c.querySelectorAll(s.trim()))).filter(vis);
    const inputs = Array.from(c.querySelectorAll('input,select,textarea'))
      .filter(i => i.type !== 'hidden' && i.type !== 'submit' && i.type !== 'button');
    const items = inputs.map(inp => {
      const qcid = 'qc' + (counter++); inp.dataset.qcid = qcid;
      const type = inp.tagName === 'SELECT' ? 'select' : inp.tagName === 'TEXTAREA' ? 'textarea' : (inp.type || 'text');
      let code = inp.value;
      if (type === 'checkbox') {
        const m = (inp.name || '').match(new RegExp('^' + name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '_(?:r)?(\\w+)$', 'i'));
        if ((code === '1' || code === 'on' || !code) && m) code = m[1];
      }
      const item = { qcid, id: inp.id, name: inp.name, type, value: inp.value, code,
                     label: (type === 'radio' || type === 'checkbox') ? labelOf(inp, c) : '',
                     col: (type === 'radio' || type === 'checkbox') ? colHeader(inp) : '',
                     row: rowLabel(inp), visible: vis(inp) || vis(inp.parentElement),
                     cls: inp.className || '', min: inp.min || '', max: inp.max || '' };
      if (type === 'select') item.choices = Array.from(inp.options).filter(o => o.value !== '')
                                 .map(o => ({ code: o.value, label: clean(o.text) }));
      return item;
    });
    let text = clean(headers.map(h => h.innerText).join(' '));
    if (!text) {
      const optText = new Set(items.map(i => i.label));
      text = clean(c.innerText.split('\n').filter(l => clean(l) && !optText.has(clean(l))).slice(0, 3).join(' '));
    }
    const errs = cfg.errorSel.split(',').flatMap(s => Array.from(c.querySelectorAll(s.trim())))
                  .filter(vis).map(e => clean(e.innerText)).filter(t => t && t.length < 400);
    return { name, text, full_text: clean(c.innerText).slice(0, 4000), items, errors: Array.from(new Set(errs)) };
  });
  const pageErrs = cfg.errorSel.split(',').flatMap(s => Array.from(document.querySelectorAll(s.trim())))
                    .filter(vis).map(e => clean(e.innerText)).filter(t => t && t.length < 400);
  return { questions: out, body: clean(document.body ? document.body.innerText : '').slice(0, 5000),
           errors: Array.from(new Set(pageErrs)), title: document.title, url: location.href };
}
"""

ACT_JS = r"""
(actions) => {
  for (const a of actions) {
    const el = document.querySelector(`[data-qcid="${a.qcid}"]`); if (!el) continue;
    if (a.op === 'check') {
      if (el.checked !== a.on) { if (a.on || el.type === 'checkbox') { try { el.click(); } catch (e) {} }
        if (el.checked !== a.on) el.checked = a.on; }
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (a.op === 'text') {
      el.focus(); el.value = a.value;
      ['input', 'change', 'keyup', 'blur'].forEach(t => el.dispatchEvent(new Event(t, { bubbles: true })));
    } else if (a.op === 'select') {
      el.value = a.value; el.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }
  return true;
}
"""

STATE_JS = r"""
(ids) => ids.map(id => { const el = document.querySelector(`[data-qcid="${id}"]`); return el ? el.checked : null; })
"""


# --------------------------------------------------------------------------
# Live-question model built from the extracted structure
# --------------------------------------------------------------------------
def summarise_live(raw: dict) -> dict:
    items = raw["items"]
    radios = [i for i in items if i["type"] == "radio"]
    checks = [i for i in items if i["type"] == "checkbox"]
    selects = [i for i in items if i["type"] == "select"]
    texts = [i for i in items if i["type"] in ("text", "number", "textarea", "tel", "email")]
    radio_names = list(dict.fromkeys(i["name"] for i in radios))
    check_rows = list(dict.fromkeys(i["row"] for i in checks if i["row"]))

    if len(radio_names) > 1:
        kind = "grid"
    elif radios:
        kind = "radio"
    elif checks and len(check_rows) > 1 and any(i["col"] for i in checks):
        kind = "grid"
    elif checks:
        kind = "checkbox"
    elif selects:
        kind = "select"
    elif texts:
        kind = "text"
    else:
        kind = "info"

    options = []
    if kind == "radio":
        options = [{"code": i["code"], "label": i["label"], "qcid": i["qcid"]} for i in radios]
    elif kind == "checkbox":
        options = [{"code": i["code"], "label": i["label"], "qcid": i["qcid"]} for i in checks]
    elif kind == "select":
        options = [{"code": c["code"], "label": c["label"]} for c in selects[0].get("choices", [])]
    elif kind == "grid":
        group = [i for i in radios if i["name"] == radio_names[0]] if radio_names else \
                [i for i in checks if i["row"] == check_rows[0]]
        options = [{"code": i["value"] if i["type"] == "radio" else i["code"],
                    "label": i["col"] or i["label"], "qcid": i["qcid"]} for i in group]
    row_labels = []
    if kind == "grid":
        if radio_names:
            for n in radio_names:
                r = next((i["row"] for i in radios if i["name"] == n and i["row"]), "")
                row_labels.append(r)
        else:
            row_labels = check_rows
    # strip option labels that leaked into the question text
    return {"name": raw["name"], "text": raw["text"], "full_text": raw["full_text"], "kind": kind,
            "options": options, "row_labels": [r for r in row_labels if r], "items": items,
            "radio_names": radio_names, "texts": texts, "selects": selects, "checks": checks,
            "radios": radios, "errors": raw.get("errors", [])}


def key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class SurveyBot:
    def __init__(self, qnr: list[dict], settings: Settings, out_dir: Path, log=print):
        self.qnr = qnr
        self.by_key = {key(q["id"]): q for q in qnr}
        self.s = settings
        self.out = Path(out_dir)
        (self.out / "screenshots").mkdir(parents=True, exist_ok=True)
        self.log = log
        self.res = RunResult()
        self.dialogs: list[str] = []
        self.page: Page | None = None

    # ---------------------------------------------------------------- helpers
    def extract(self) -> dict:
        cfg = {"container": SELECTORS["container"], "container_fallback": SELECTORS["container_fallback"],
               "textSel": ", ".join(SELECTORS["question_text"]), "errorSel": ", ".join(SELECTORS["errors"])}
        data = self.page.evaluate(EXTRACT_JS, cfg)
        data["live"] = [summarise_live(q) for q in data["questions"]]
        return data

    def match_qnr(self, live: dict, used: set) -> tuple[dict | None, str]:
        q = self.by_key.get(key(live["name"]))
        if q:
            return q, "name"
        # loop / suffix variants: Q5_1 -> Q5, Q5r1 -> Q5
        base = re.sub(r"(_\w+|r\d+)$", "", live["name"], flags=re.I)
        if base != live["name"] and key(base) in self.by_key:
            return self.by_key[key(base)], "name (base)"
        best, best_s = None, 0
        lt = norm(live["text"])
        if not lt:
            return None, ""
        for q in self.qnr:
            if q["id"] in used or not q.get("text"):
                continue
            s = fuzz.token_set_ratio(norm(q["text"]), lt)
            if s > best_s:
                best, best_s = q, s
        if best and best_s >= 88:
            return best, f"text ({best_s:.0f}%)"
        return None, ""

    def find_next(self):
        for sel in SELECTORS["next"]:
            loc = self.page.locator(sel)
            try:
                if loc.count() and loc.first.is_visible():
                    return loc.first
            except Exception:
                continue
        return None

    def signature(self, data: dict) -> str:
        names = ",".join(sorted(q["name"] for q in data["live"]))
        return names + "|" + norm(data["body"])[:300] if not names else names

    def submit(self, sig_before: str) -> tuple[bool, list[str]]:
        """Click Next. Returns (advanced, error_messages)."""
        self.dialogs.clear()
        btn = self.find_next()
        if btn is None:
            return False, ["Next button not found"]
        try:
            self.page.evaluate("() => { window.__qc_marker = 1; }")
        except Exception:
            pass
        try:
            btn.click(timeout=5000)
        except Exception as e:
            return False, [f"Could not click Next: {e}"]
        start = time.time()
        reloaded = False
        while time.time() - start < self.s.nav_timeout:
            time.sleep(0.3)
            try:
                marker = self.page.evaluate("() => window.__qc_marker === 1")
            except Exception:
                continue  # navigation in progress
            if not marker:
                reloaded = True
                break
            if self.dialogs and time.time() - start > 0.6:
                break
            try:
                errs = self.page.evaluate(
                    "(sel) => sel.split(',').flatMap(s => Array.from(document.querySelectorAll(s.trim())))"
                    ".filter(e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')"
                    ".map(e => e.innerText.trim()).filter(Boolean)", ", ".join(SELECTORS["errors"]))
            except Exception:
                continue
            if errs and time.time() - start > 1.0:
                return False, list(dict.fromkeys(e for e in errs if len(e) < 400)) + self.dialogs
        if not reloaded:
            return False, list(self.dialogs)
        try:
            self.page.wait_for_load_state("load", timeout=20000)
        except Exception:
            pass
        data = self.extract()
        if self.signature(data) == sig_before:
            # server-side validation re-rendered the same page
            errs = data["errors"] + [e for q in data["live"] for e in q["errors"]]
            return False, list(dict.fromkeys(errs)) + list(self.dialogs) or ["Same page re-shown"]
        return True, []

    def act(self, actions: list[dict]):
        if actions:
            self.page.evaluate(ACT_JS, actions)

    # ---------------------------------------------------------- answer logic
    def clear_actions(self, lq: dict) -> list[dict]:
        acts = []
        for i in lq["items"]:
            if i["type"] in ("radio", "checkbox"):
                acts.append({"op": "check", "qcid": i["qcid"], "on": False})
            elif i["type"] == "select":
                acts.append({"op": "select", "qcid": i["qcid"], "value": ""})
            else:
                acts.append({"op": "text", "qcid": i["qcid"], "value": ""})
        return acts

    @staticmethod
    def _flags(q: dict | None) -> tuple[set, set]:
        if not q:
            return set(), set()
        term = {str(o["code"]) for o in q["options"] if o.get("terminate")}
        excl = {str(o["code"]) for o in q["options"] if o.get("exclusive")}
        return term, excl

    def _choose(self, opts: list[dict], q: dict | None, n: int = 1) -> list[dict]:
        term, excl = self._flags(q)
        good = [o for o in opts if str(o["code"]) not in term and str(o["code"]) not in excl
                and not AVOID_LABEL.search(o.get("label", ""))]
        ok = [o for o in opts if str(o["code"]) not in term and str(o["code"]) not in excl]
        pool = good or ok or opts
        # deterministic by code so a randomised display order never changes the route
        pool = sorted(pool, key=lambda o: (0, int(o["code"])) if str(o["code"]).isdigit() else (1, str(o["code"])))
        return pool[:max(1, n)]

    def valid_number(self, q: dict | None, override=None) -> str:
        if override not in (None, ""):
            return str(override)
        v = (q or {}).get("validation", {})
        lo, hi = v.get("min"), v.get("max")
        if lo is not None and hi is not None:
            val = lo if lo > 0 else min(hi, max(lo, 1))
        elif lo is not None:
            val = lo
        elif hi is not None:
            val = min(1, hi)
        else:
            val = 5
        return str(int(val) if float(val).is_integer() else val)

    def valid_actions(self, lq: dict, q: dict | None) -> tuple[list[dict], str]:
        acts = self.clear_actions(lq)
        override = (q or {}).get("answer")
        desc = ""
        kind = lq["kind"]
        qtype = (q or {}).get("type")
        if kind == "radio":
            opts = lq["options"]
            pick = [o for o in opts if override is not None and str(o["code"]) == str(override)] or self._choose(opts, q)
            acts.append({"op": "check", "qcid": pick[0]["qcid"], "on": True})
            desc = f"{pick[0]['code']} ({pick[0]['label'][:40]})"
        elif kind == "checkbox":
            opts = lq["options"]
            if override is not None:
                want = {str(x) for x in (override if isinstance(override, list) else [override])}
                pick = [o for o in opts if str(o["code"]) in want]
            else:
                n = int(((q or {}).get("validation") or {}).get("min_select") or 1)
                pick = self._choose(opts, q, n)
            for o in pick:
                acts.append({"op": "check", "qcid": o["qcid"], "on": True})
            desc = ", ".join(str(o["code"]) for o in pick)
        elif kind == "select":
            sel = lq["selects"][0]
            choices = sel.get("choices", [])
            pick = [c for c in choices if override is not None and str(c["code"]) == str(override)] or self._choose(choices, q)
            if pick:
                acts.append({"op": "select", "qcid": sel["qcid"], "value": pick[0]["code"]})
                desc = f"{pick[0]['code']} ({pick[0]['label'][:40]})"
        elif kind == "grid":
            picks = []
            if lq["radio_names"]:
                for idx, name in enumerate(lq["radio_names"]):
                    group = [{"code": i["value"], "label": i["col"] or i["label"], "qcid": i["qcid"]}
                             for i in lq["radios"] if i["name"] == name]
                    want = override.get(str(idx + 1)) if isinstance(override, dict) else override
                    pick = [o for o in group if want is not None and str(o["code"]) == str(want)] or self._choose(group, q)
                    acts.append({"op": "check", "qcid": pick[0]["qcid"], "on": True})
                    picks.append(str(pick[0]["code"]))
            else:
                rows = list(dict.fromkeys(i["row"] for i in lq["checks"]))
                for r in rows:
                    first = next(i for i in lq["checks"] if i["row"] == r)
                    acts.append({"op": "check", "qcid": first["qcid"], "on": True})
                    picks.append(first["code"])
            desc = "rows: " + ", ".join(picks)
        if lq["texts"] and kind in ("text", "info"):
            vals = []
            for t in lq["texts"]:
                if qtype == "open":
                    v = str(override) if override not in (None, "") else "QC test response"
                elif qtype == "numeric" or t["type"] == "number" or "numeric" in t["cls"].lower():
                    v = self.valid_number(q, override)
                else:
                    v = str(override) if override not in (None, "") else "QC test"
                acts.append({"op": "text", "qcid": t["qcid"], "value": v})
                vals.append(v)
            desc = ", ".join(vals)
        return acts, desc

    def fill_page(self, data: dict, matches: dict) -> list[str]:
        acts, answers = [], []
        for lq in data["live"]:
            q = matches.get(lq["name"])
            a, d = self.valid_actions(lq, q)
            acts.extend(a)
            if d:
                answers.append(f"{lq['name']}={d}")
        self.act(acts)
        return answers

    # ---------------------------------------------------------- validations
    def plan_tests(self, lq: dict, q: dict) -> list[dict]:
        tests = []
        v = q["validation"]
        kind = lq["kind"]
        if kind == "info":
            return tests
        clear = self.clear_actions(lq)
        if v.get("required") and q["type"] != "info":
            tests.append({"name": "Required (left blank)", "input": "no answer",
                          "actions": clear, "expect": "blocked"})
        if q["type"] == "numeric" and lq["texts"]:
            t = lq["texts"][0]
            if v.get("min") is not None:
                bad = v["min"] - 1
                tests.append({"name": f"Below minimum ({v['min']})", "input": str(bad),
                              "actions": clear + [{"op": "text", "qcid": t["qcid"], "value": str(bad)}],
                              "expect": "blocked"})
            if v.get("max") is not None:
                bad = v["max"] + 1
                tests.append({"name": f"Above maximum ({v['max']})", "input": str(bad),
                              "actions": clear + [{"op": "text", "qcid": t["qcid"], "value": str(bad)}],
                              "expect": "blocked"})
            tests.append({"name": "Non-numeric input", "input": "abc",
                          "actions": clear + [{"op": "text", "qcid": t["qcid"], "value": "abc"}],
                          "expect": "blocked"})
        if kind == "checkbox":
            term, excl = self._flags(q)
            normal = [o for o in lq["options"] if str(o["code"]) not in excl]
            if v.get("max_select") and len(normal) > v["max_select"]:
                n = int(v["max_select"]) + 1
                tests.append({"name": f"More than max selections ({v['max_select']})",
                              "input": ", ".join(str(o["code"]) for o in normal[:n]),
                              "actions": clear + [{"op": "check", "qcid": o["qcid"], "on": True} for o in normal[:n]],
                              "expect": "blocked"})
            if v.get("min_select") and v["min_select"] > 1:
                n = int(v["min_select"]) - 1
                tests.append({"name": f"Fewer than min selections ({v['min_select']})",
                              "input": ", ".join(str(o["code"]) for o in normal[:n]),
                              "actions": clear + [{"op": "check", "qcid": o["qcid"], "on": True} for o in normal[:n]],
                              "expect": "blocked"})
            for o in lq["options"]:
                if str(o["code"]) in excl and normal:
                    other = normal[0]
                    tests.append({"name": f"Exclusive option {o['code']} with another",
                                  "input": f"{other['code']} + {o['code']}",
                                  "actions": clear + [{"op": "check", "qcid": other["qcid"], "on": True},
                                                      {"op": "check", "qcid": o["qcid"], "on": True}],
                                  "expect": "blocked_or_cleared", "watch": [other["qcid"], o["qcid"]]})
        if kind == "grid" and lq["radio_names"] and v.get("required") and len(lq["radio_names"]) > 1:
            # answer every row except the last
            acts, _ = self.valid_actions(lq, q)
            last = lq["radio_names"][-1]
            last_ids = {i["qcid"] for i in lq["radios"] if i["name"] == last}
            acts = [a for a in acts if a.get("qcid") not in last_ids or not a.get("on")]
            tests.append({"name": "Grid row left blank", "input": f"row {len(lq['radio_names'])} empty",
                          "actions": acts, "expect": "blocked"})
        return tests

    def page_plans(self, data: dict, matches: dict) -> list:
        plans = []
        for lq in data["live"]:
            q = matches.get(lq["name"])
            if q:
                plans.extend((lq, q, t) for t in self.plan_tests(lq, q))
        return plans

    def run_tests(self, page_no: int, data: dict, matches: dict, start: int = 0) -> tuple[bool, int]:
        """Run negative tests from index `start`. Returns (page_advanced, next_index)."""
        sig = self.signature(data)
        plans = self.page_plans(data, matches)
        for n in range(start, len(plans)):
            lq, q, t = plans[n]
            self.fill_page(data, matches)
            self.act(t["actions"])
            row = {"page": page_no, "question": q["id"], "test": t["name"], "input": t["input"],
                   "expected": "Error / blocked", "result": "", "message": "", "status": ""}
            if t["expect"] == "blocked_or_cleared":
                state = self.page.evaluate(STATE_JS, t["watch"])
                if not all(state):
                    row.update(result="Other option auto-deselected", status=PASS)
                    self.res.validations.append(row)
                    self.log(f"  PASS {q['id']}: {t['name']}")
                    continue
            advanced, msgs = self.submit(sig)
            if advanced:
                row.update(result="Page advanced (accepted invalid input)", status=FAIL)
                self.res.validations.append(row)
                self.log(f"  FAIL {q['id']}: {t['name']} was accepted")
                return True, n + 1
            row.update(result="Blocked", message=" | ".join(msgs)[:500], status=PASS if msgs else WARN)
            if not msgs:
                row["result"] = "Did not advance, but no error message detected"
            self.res.validations.append(row)
            self.log(f"  {row['status']} {q['id']}: {t['name']}")
        return False, len(plans)

    def mark_untested(self, page_no: int, plans: list, start: int, why: str):
        for _, q, t in plans[start:]:
            self.res.validations.append({"page": page_no, "question": q["id"], "test": t["name"],
                                         "input": t["input"], "expected": "Error / blocked",
                                         "result": f"Not tested ({why})", "message": "", "status": WARN})

    def new_session(self, url: str):
        if getattr(self, "ctx", None):
            try:
                self.ctx.close()
            except Exception:
                pass
        self.ctx = self.browser.new_context(viewport={"width": self.s.viewport[0], "height": self.s.viewport[1]})
        self.page = self.ctx.new_page()
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.accept()))
        self.page.goto(url, wait_until="load", timeout=60000)

    def replay_to(self, url: str, target_page: int, target_sig: str) -> bool:
        """Start a fresh respondent and answer validly up to target_page."""
        self.log(f"  Replaying link to page {target_page} to finish its tests...")
        self.new_session(url)
        for p in range(1, target_page):
            data = self.extract()
            matches = {lq["name"]: q for lq in data["live"] for q, _ in [self.match_qnr(lq, set())] if q}
            self.fill_page(data, matches)
            ok, _ = self.submit(self.signature(data))
            if not ok:
                self.broad_fill(data)
                ok, _ = self.submit(self.signature(data))
                if not ok:
                    return False
        return self.signature(self.extract()) == target_sig

    # ---------------------------------------------------------------- main
    def shot(self, page_no: int, suffix: str = "") -> str:
        p = self.out / "screenshots" / f"page_{page_no:03d}{suffix}.png"
        try:
            self.page.screenshot(path=str(p), full_page=True)
        except Exception:
            return ""
        return str(p.relative_to(self.out))

    def run(self, url: str) -> RunResult:
        with sync_playwright() as pw:
            self.browser = pw.chromium.launch(headless=self.s.headless, slow_mo=self.s.slow_mo)
            browser = self.browser
            self.log(f"Opening {url}")
            self.new_session(url)
            used: set = set()
            page_no = 0
            try:
                while page_no < self.s.max_pages:
                    page_no += 1
                    data = self.extract()
                    nxt = self.find_next()
                    names = [lq["name"] for lq in data["live"]]
                    self.log(f"Page {page_no}: {', '.join(names) or '(no questions)'}")
                    screenshot = self.shot(page_no)

                    if nxt is None:
                        self.finish(data, page_no, screenshot)
                        break

                    matches = {}
                    for lq in data["live"]:
                        q, how = self.match_qnr(lq, used)
                        if q:
                            matches[lq["name"]] = q
                            used.add(q["id"])
                            self.res.seen.setdefault(q["id"], page_no)
                            for f in compare_question(q, lq, self.s.pass_at, self.s.warn_at, self.s.strict):
                                f.update(page=page_no, live_name=lq["name"], matched_by=how)
                                self.res.findings.append(f)
                        elif lq["kind"] != "info":
                            self.res.findings.append({
                                "page": page_no, "question": lq["name"], "live_name": lq["name"],
                                "matched_by": "", "check": "Question in QNR", "qnr": "",
                                "live": lq["text"], "similarity": None, "status": WARN,
                                "note": "Question on link could not be matched to any QNR question"})

                    if self.s.run_validations:
                        sig0 = self.signature(data)
                        advanced, nxt_i = self.run_tests(page_no, data, matches)
                        while advanced:
                            plans = self.page_plans(data, matches)
                            if nxt_i >= len(plans) and not self.s.replay_on_fail:
                                break
                            if not self.s.replay_on_fail:
                                self.mark_untested(page_no, plans, nxt_i, "page advanced on an earlier failed test")
                                break
                            if not self.replay_to(url, page_no, sig0):
                                self.mark_untested(page_no, plans, nxt_i, "could not return to this page on replay")
                                self.res.end_state = f"Stopped: replay could not return to page {page_no}"
                                raise StopIteration
                            data = self.extract()
                            if nxt_i >= len(plans):
                                advanced = False
                                break
                            advanced, nxt_i = self.run_tests(page_no, data, matches, nxt_i)
                        if advanced:  # replay disabled: already past this page
                            self.res.path.append({"page": page_no, "questions": ", ".join(names),
                                                  "answers": "(advanced during validation test)",
                                                  "screenshot": screenshot})
                            continue

                    answers = self.fill_page(data, matches)
                    sig = self.signature(data)
                    advanced, msgs = self.submit(sig)
                    if not advanced:
                        self.log("  Valid answers were rejected, retrying with a broader fill")
                        self.broad_fill(data)
                        advanced, msgs = self.submit(sig)
                    self.res.path.append({"page": page_no, "questions": ", ".join(names),
                                          "answers": "; ".join(answers), "screenshot": screenshot})
                    if not advanced:
                        self.shot(page_no, "_stuck")
                        self.res.findings.append({
                            "page": page_no, "question": ", ".join(names), "live_name": "", "matched_by": "",
                            "check": "Page submit with valid answers", "qnr": "accepted",
                            "live": " | ".join(msgs)[:500], "similarity": None, "status": FAIL,
                            "note": "Bot could not move past this page with valid answers. Check the validation "
                                    "message, or set an 'answer' for these questions in the QNR JSON."})
                        self.res.end_state = f"Stuck on page {page_no}"
                        break
                else:
                    self.res.end_state = f"Stopped after max pages ({self.s.max_pages})"
            except StopIteration:
                self.log(self.res.end_state)
            except Exception as e:
                self.res.errors.append(repr(e))
                self.res.end_state = f"Error on page {page_no}: {e}"
                self.shot(page_no, "_error")
                self.log(f"ERROR: {e}")
            finally:
                browser.close()
        return self.res

    def broad_fill(self, data: dict):
        acts = []
        for lq in data["live"]:
            for t in lq["texts"]:
                acts.append({"op": "text", "qcid": t["qcid"], "value": "5"})
            for n in lq["radio_names"]:
                first = next(i for i in lq["radios"] if i["name"] == n)
                acts.append({"op": "check", "qcid": first["qcid"], "on": True})
        self.act(acts)

    def finish(self, data: dict, page_no: int, screenshot: str):
        body = data["body"]
        if END_TERM.search(body):
            self.res.end_state = "Terminated / screened out"
        elif END_COMPLETE.search(body):
            self.res.end_state = "Completed"
        else:
            self.res.end_state = "Ended (no Next button)"
        self.res.end_text = body[:500]
        self.res.path.append({"page": page_no, "questions": "(end page)", "answers": "",
                              "screenshot": screenshot})
        self.log(f"End reached: {self.res.end_state}")


def inspect_page(url: str, headless: bool = True) -> dict:
    """Open only the first page and return what the bot detects (for troubleshooting selectors)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(url, wait_until="load", timeout=60000)
        bot = SurveyBot([], Settings(), Path(tempfile.mkdtemp()), log=lambda *_: None)
        bot.page = page
        data = bot.extract()
        nxt = bot.find_next()
        browser.close()
    return {"url": data["url"], "next_button_found": nxt is not None,
            "questions": [{"name": lq["name"], "kind": lq["kind"], "text": lq["text"],
                           "options": [{"code": o["code"], "label": o["label"]} for o in lq["options"]],
                           "rows": lq["row_labels"], "text_inputs": len(lq["texts"])} for lq in data["live"]]}
