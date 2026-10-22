"""
Survey Link QC - Streamlit app.

    streamlit run app.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from qnr_parser import DEFAULT_MODEL, parse_qnr
from qnr_table import df_to_qnr, qnr_to_df

HERE = Path(__file__).parent
RUNS = HERE / "runs"

st.set_page_config(page_title="Survey Link QC", page_icon="✅", layout="wide")
st.title("Survey Link QC")
st.caption("Checks a Sawtooth Lighthouse live link against the Word questionnaire: "
           "question text, answer options and validations.")

# --------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("Anthropic API key", value=os.getenv("ANTHROPIC_API_KEY", ""), type="password",
                            help="Used only to read the QNR. Leave empty to use the offline rule-based parser.")
    model = st.text_input("Claude model", value=DEFAULT_MODEL)
    st.divider()
    headed = st.toggle("Show the browser while testing", value=False)
    run_val = st.toggle("Run validation tests", value=True,
                        help="Blank answers, out-of-range numbers, too many selections, exclusive options.")
    strict = st.toggle("Strict wording check", value=True,
                       help="On: any word change is a FAIL. Off: scored by similarity.")
    replay = st.toggle("Replay to finish skipped tests", value=True,
                       help="If an invalid answer is accepted, restart the link and come back to finish "
                            "that page's remaining tests.")
    max_pages = st.number_input("Max pages", 5, 1000, 150)
    nav_timeout = st.number_input("Seconds to wait for a page", 2.0, 60.0, 6.0, 0.5)


# ------------------------------------------------------------- step 1: QNR
st.subheader("1. Questionnaire")
c1, c2 = st.columns(2)
with c1:
    docx_file = st.file_uploader("Word questionnaire (.docx)", type=["docx"])
    if docx_file and st.button("Read questionnaire", type="primary"):
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(docx_file.getvalue())
        msgs = st.empty()
        with st.spinner("Reading the questionnaire..."):
            qnr = parse_qnr(tmp.name, api_key or None, model, log=lambda m: msgs.info(m))
        st.session_state.qnr = qnr
        st.session_state.qnr_name = docx_file.name
        msgs.success(f"Found {len(qnr)} questions" + ("" if api_key else " (rule-based parser)"))
with c2:
    js = st.file_uploader("...or load a saved QNR file (.json)", type=["json"])
    if js and st.button("Load saved QNR"):
        st.session_state.qnr = json.loads(js.getvalue())
        st.session_state.qnr_name = js.name

if "qnr" in st.session_state:
    st.subheader("2. Review what was read")
    st.caption("Fix anything the reader got wrong before testing. Options are written as code=label separated "
               "by |, with [T] for terminate and [E] for exclusive. 'answer' tells the bot what to pick "
               "(use it to walk a different route or avoid a quota); leave blank for automatic.")
    edited = st.data_editor(
        qnr_to_df(st.session_state.qnr), num_rows="dynamic", width="stretch", height=420,
        column_config={
            "type": st.column_config.SelectboxColumn(options=["single", "multi", "numeric", "open", "grid", "info"]),
            "text": st.column_config.TextColumn(width="large"),
            "options": st.column_config.TextColumn(width="large"),
            "required": st.column_config.CheckboxColumn(),
            "randomize": st.column_config.CheckboxColumn(),
        }, key="qnr_editor")
    qnr = df_to_qnr(edited)
    st.download_button("Save reviewed QNR (.json)", json.dumps(qnr, indent=2, ensure_ascii=False),
                       file_name=Path(st.session_state.get("qnr_name", "qnr")).stem + "_parsed.json",
                       mime="application/json")

    # ------------------------------------------------------- step 3: run
    st.subheader("3. Test the live link")
    url = st.text_input("Live / test link", placeholder="https://.../cgi-bin/ciwweb.pl?studyname=...")
    b1, b2 = st.columns([1, 4])
    inspect = b1.button("Inspect first page", disabled=not url,
                        help="Shows what the bot detects on page 1. Use this the first time on a new template.")
    run = b2.button("Run QC", type="primary", disabled=not url)

    if inspect:
        with st.spinner("Opening the link..."):
            p = subprocess.run([sys.executable, str(HERE / "run_qc.py"), "--url", url, "--inspect"],
                               capture_output=True, text=True, cwd=HERE)
        if p.returncode == 0:
            info = json.loads(p.stdout)
            st.write(f"Next button found: **{info['next_button_found']}** | "
                     f"Questions found: **{len(info['questions'])}**")
            st.json(info["questions"])
        else:
            st.error(p.stderr[-2000:])

    if run:
        run_dir = RUNS / datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "qnr.json").write_text(json.dumps(qnr, indent=2, ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, "-u", str(HERE / "run_qc.py"), "--url", url, "--qnr", str(run_dir / "qnr.json"),
               "--qnr-name", st.session_state.get("qnr_name", "qnr"), "--out", str(run_dir),
               "--max-pages", str(int(max_pages)), "--nav-timeout", str(nav_timeout)]
        if headed:
            cmd.append("--headed")
        if not run_val:
            cmd.append("--no-validations")
        if not strict:
            cmd.append("--lenient")
        if not replay:
            cmd.append("--no-replay")
        log_box = st.empty()
        lines = []
        with st.spinner("Testing the link..."):
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=HERE)
            for line in proc.stdout:
                lines.append(line.rstrip())
                log_box.code("\n".join(lines[-25:]))
            proc.wait()
        if (run_dir / "QC_Report.xlsx").exists():
            st.session_state.last_run = str(run_dir)
        else:
            st.error("The run did not finish. Log:\n\n" + "\n".join(lines[-40:]))

    # -------------------------------------------------------- results
    if st.session_state.get("last_run"):
        run_dir = Path(st.session_state.last_run)
        res = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        st.subheader("Results")
        f = pd.DataFrame(res["findings"])
        v = pd.DataFrame(res["validations"])
        m = st.columns(5)
        m[0].metric("End state", res["end_state"])
        m[1].metric("Pages", len(res["path"]))
        m[2].metric("Text/option FAILs", int((f["status"] == "FAIL").sum()) if len(f) else 0)
        m[3].metric("Validation FAILs", int((v["status"] == "FAIL").sum()) if len(v) else 0)
        reached = len(res["seen"])
        m[4].metric("QNR questions reached", f"{reached}/{len([q for q in qnr if q['type'] != 'info'])}")

        t1, t2, t3 = st.tabs(["Text & options issues", "Validation tests", "Not reached"])
        with t1:
            if len(f):
                show = f[f["status"] != "PASS"][["page", "question", "check", "qnr", "live", "status", "note"]]
                st.dataframe(show, width="stretch", hide_index=True)
        with t2:
            if len(v):
                st.dataframe(v[["page", "question", "test", "input", "result", "message", "status"]],
                             width="stretch", hide_index=True)
        with t3:
            nr = [{"id": q["id"], "text": q["text"], "notes": q.get("notes", "")} for q in qnr
                  if q["id"] not in res["seen"] and q["type"] != "info"]
            st.dataframe(pd.DataFrame(nr), width="stretch", hide_index=True)
            st.caption("Usually routing. Set 'answer' values in step 2 and run again to walk that path.")

        d1, d2 = st.columns(2)
        d1.download_button("Download QC report (.xlsx)", (run_dir / "QC_Report.xlsx").read_bytes(),
                           file_name=f"QC_Report_{run_dir.name}.xlsx", type="primary")
        if (run_dir / "screenshots.zip").exists():
            d2.download_button("Download screenshots (.zip)", (run_dir / "screenshots.zip").read_bytes(),
                               file_name=f"screenshots_{run_dir.name}.zip")
