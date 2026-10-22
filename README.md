# Survey Link QC

Checks a Sawtooth Lighthouse Studio live/test link against the Word questionnaire (QNR) and
produces an Excel QC report.

## What it checks (v1)

| Area | Checks |
|---|---|
| Question text | Live wording vs QNR. Strict mode: any word change is a FAIL, punctuation-only is a WARN, extra text around an exact match is INFO |
| Answer options | Missing options, extra options, label wording, code mismatches, option order (INFO if the QNR says randomise) |
| Question type | Single vs multi vs numeric vs open vs grid |
| Grids | Row statements and scale columns |
| Validations | Left blank (required), below min / above max, non-numeric, more than max selections, fewer than min selections, exclusive option combined with another, grid row left blank |
| Coverage | Which QNR questions were never reached on the path the bot took |

Every page is screenshotted, and the report logs what the bot answered on each page.

## Setup (once per machine)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Optional: set `ANTHROPIC_API_KEY` (or paste it in the app sidebar). With a key, Claude reads the QNR,
which handles messy layouts, grids and programmer notes much better. Without one, a rule-based reader
is used that works for cleanly formatted QNRs (`Q1. text [SINGLE CODE]`, `1. option`, option tables).

## Run

```bash
streamlit run app.py
```

1. Upload the Word QNR and click **Read questionnaire**.
2. Review the table. Fix anything misread. Options are `code=label | code=label`, with `[T]` for terminate
   and `[E]` for exclusive. Save the reviewed QNR as JSON so you can reuse it for later runs.
3. Paste the link. First time on a new template, click **Inspect first page** to confirm the bot sees the
   questions and the Next button. Then **Run QC** and download the report.

Command line (same engine):

```bash
python run_qc.py --qnr reviewed_qnr.json --url "https://..." --out runs/test1
python run_qc.py --url "https://..." --inspect
```

Flags: `--headed` (watch the browser), `--no-validations`, `--lenient`, `--no-replay`, `--max-pages`, `--nav-timeout`.

## How it walks the survey

On each page it: reads every question container (`<div id="Q1_div" class="question ...">`), matches it to
the QNR by question name (falls back to text similarity), compares, then runs the invalid-input tests. A
correctly programmed page should block each one. If an invalid answer is accepted, that is logged as a
FAIL, and the bot starts a fresh respondent, answers its way back to the same page and finishes the
remaining tests ("replay"). Finally it answers validly and moves on.

Valid answers avoid options marked terminate, exclusive, "Other/specify" and "Don't know", and pick by
code order so randomised lists don't change the route between runs.

## Testing other routes

One run follows one path. Questions behind skip logic appear on the **Coverage** sheet as not reached.
To walk another path, fill the `answer` column in step 2 (for example `Q1 = 4` to reach a "Brand D"
follow-up, `1,3` for a multi, a number for numeric) and run again.

## Reading the report

- **Summary**: end state (Completed / Terminated / Stuck), pass/fail counts.
- **Text & Options**: one row per check, with a word diff in the Note column.
- **Validations**: each test, the input used, and the error message the link showed.
- **Coverage**: QNR questions reached or not.
- **Path log**: what the bot answered on each page, with screenshot file names.

## Adjusting for custom templates

All page selectors are in `SELECTORS` at the top of `sawtooth_bot.py` (question containers, header text,
Next button, error messages). If **Inspect first page** finds no questions or no Next button, adjust those.

## Known limits of v1

- Skip logic, terminations and quotas are not verified yet (only coverage is reported).
- Piped text (`[INSERT BRAND]`) shows as a wording difference; check those rows manually.
- Custom HTML/JS questions (drag and drop, sliders, card sorts, heatmaps) are reported as unmatched or
  without options and are answered with a generic fill, which may not satisfy their validation.
- If the link starts with a password page, use a test link that skips it or include the passwords in the URL.

## Testing the tool itself

`testing/mock_survey.py` serves a fake Lighthouse-style survey with planted errors, and
`testing/sample_qnr.docx` is the matching QNR:

```bash
python testing/mock_survey.py            # http://localhost:5055/survey
python run_qc.py --qnr testing/sample_qnr.json --url http://localhost:5055/survey --out runs/mock
```
