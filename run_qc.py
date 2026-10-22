"""
Command-line runner (also used by the Streamlit app as a subprocess).

  python run_qc.py --qnr parsed_qnr.json --url "https://..." --out runs/test1
  python run_qc.py --url "https://..." --inspect
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from report import build_report
from sawtooth_bot import Settings, SurveyBot, inspect_page


def main():
    ap = argparse.ArgumentParser(description="QC a Sawtooth live link against a parsed QNR")
    ap.add_argument("--url", required=True)
    ap.add_argument("--qnr", help="Parsed QNR JSON (from the app or qnr_parser.py)")
    ap.add_argument("--qnr-name", default="")
    ap.add_argument("--out", default="qc_run")
    ap.add_argument("--headed", action="store_true", help="Show the browser while testing")
    ap.add_argument("--no-validations", action="store_true")
    ap.add_argument("--max-pages", type=int, default=150)
    ap.add_argument("--pass-at", type=float, default=95)
    ap.add_argument("--warn-at", type=float, default=80)
    ap.add_argument("--slow-mo", type=int, default=0)
    ap.add_argument("--nav-timeout", type=float, default=6.0)
    ap.add_argument("--lenient", action="store_true", help="Score wording by similarity instead of exact words")
    ap.add_argument("--no-replay", action="store_true", help="Do not restart the link to finish skipped tests")
    ap.add_argument("--inspect", action="store_true", help="Only show what the bot detects on page 1")
    a = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    if a.inspect:
        print(json.dumps(inspect_page(a.url, headless=not a.headed), indent=2, ensure_ascii=False))
        return

    if not a.qnr:
        sys.exit("--qnr is required unless --inspect is used")
    qnr = json.loads(Path(a.qnr).read_text(encoding="utf-8"))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    s = Settings(headless=not a.headed, run_validations=not a.no_validations, max_pages=a.max_pages,
                 pass_at=a.pass_at, warn_at=a.warn_at, slow_mo=a.slow_mo, nav_timeout=a.nav_timeout,
                 strict=not a.lenient, replay_on_fail=not a.no_replay)
    res = SurveyBot(qnr, s, out, log).run(a.url)
    report = build_report(res, qnr, a.url, a.qnr_name or Path(a.qnr).name, out / "QC_Report.xlsx")
    (out / "results.json").write_text(json.dumps(asdict(res), indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.make_archive(str(out / "screenshots"), "zip", out / "screenshots")
    log(f"REPORT::{report}")


if __name__ == "__main__":
    main()
