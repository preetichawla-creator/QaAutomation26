"""Converts the parsed QNR to/from the editable review table."""
import pandas as pd


def _opts_to_str(opts):
    out = []
    for o in opts:
        flag = (" [T]" if o.get("terminate") else "") + (" [E]" if o.get("exclusive") else "")
        out.append(f"{o['code']}={o['label']}{flag}")
    return " | ".join(out)


def _str_to_opts(s, with_flags=True):
    opts = []
    for part in [p.strip() for p in str(s or "").split("|") if p.strip()]:
        code, _, label = part.partition("=")
        label = label.strip()
        term, excl = "[T]" in label, "[E]" in label
        label = label.replace("[T]", "").replace("[E]", "").strip()
        o = {"code": code.strip(), "label": label}
        if with_flags:
            o.update(terminate=term, exclusive=excl)
        opts.append(o)
    return opts


def qnr_to_df(qnr):
    return pd.DataFrame([{
        "id": q["id"], "type": q["type"], "text": q["text"],
        "options": _opts_to_str(q["options"]),
        "rows": " | ".join(f"{r['code']}={r['label']}" for r in q["rows"]),
        "required": q["validation"]["required"],
        "min": q["validation"]["min"], "max": q["validation"]["max"],
        "min_select": q["validation"]["min_select"], "max_select": q["validation"]["max_select"],
        "randomize": q["randomize"],
        "answer": "" if q.get("answer") is None else (",".join(map(str, q["answer"]))
                                                      if isinstance(q["answer"], list) else str(q["answer"])),
        "notes": q.get("notes", ""),
    } for q in qnr])


def _n(v):
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    f = float(v)
    return int(f) if f.is_integer() else f


def df_to_qnr(df):
    qnr = []
    for _, r in df.iterrows():
        if not str(r["id"]).strip():
            continue
        ans = str(r.get("answer") or "").strip()
        answer = None
        if ans:
            answer = [a.strip() for a in ans.split(",")] if "," in ans and r["type"] == "multi" else ans
        qnr.append({
            "id": str(r["id"]).strip(), "type": r["type"], "text": str(r["text"] or ""),
            "options": _str_to_opts(r["options"]), "rows": _str_to_opts(r["rows"], with_flags=False),
            "validation": {"required": bool(r["required"]), "min": _n(r["min"]), "max": _n(r["max"]),
                           "min_select": _n(r["min_select"]), "max_select": _n(r["max_select"])},
            "randomize": bool(r["randomize"]), "answer": answer, "notes": str(r.get("notes") or ""),
        })
    return qnr
