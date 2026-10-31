# OpsFleet POC — Issues & Suggestions

## Critical

1. **A live Gemini API key is in Git history and on GitHub.**
   `diagrams/OpsFleet-API-Key-Details.png` is a screenshot of the key. It was committed in
   `df0bb8d` and is on `origin/main` and `origin/dev`. **Revoke and rotate the key now.** Then
   delete the image and purge it from history (e.g. `git filter-repo`) before sharing the repo.
   The untracked `poc-assignment.pdf` at the repo root contains the same key, so do not commit it.

## Functional gaps against the assignment

2. **No natural-language understanding.** Questions must match a fixed phrase grammar of 3
   metrics × 3 groupings plus filters. The assignment's examples ("why is state X
   underspending", churn, Q1 report, product X vs Y) cannot be asked.
3. **Gemini adds cost but almost no value in the analysis path.** It is given the exact SQL and
   must echo the evidence rows unchanged. Each turn spends 2+ model calls (from a 20/day quota)
   on work the code could do deterministically. Suggestion: have the LLM write the narrative
   while the code checks every number, or let it generate SQL for real within the validator.
4. **Only `order_items` and `products` are used.** `orders` and `users` are never queried, so
   there are no state, demographic or customer-behaviour analyses.
5. **Schema questions are unreachable.** `classify` is hard-wired to `analysis`, so the
   `schema_answer` node never runs. The assignment requires "what data is available?".
6. **No multi-step analysis.** Each turn runs one query. Reports stitch together cached results
   and do not plan their own queries.
7. **Reports have no LLM insight.** Findings and action items are templates, which is safe but
   thin for an "insights and action items" report.

## Robustness

8. **Self-correction rarely triggers.** If Gemini returns SQL that differs from the approved
   SQL, or rows with re-formatted numbers, the code raises `PolicyDenied` and the turn fails.
   It is not a `RepairableQueryError`, so no retry happens. Empty results are accepted as
   valid, with no diagnostic retry.
9. **Fragile JSON parsing.** `plan` and `check_output` call `json.loads` on raw model text.
   Output wrapped in a ```json fence fails the turn. Suggestion: set
   `responseMimeType: "application/json"` (and a `responseSchema`) in `generationConfig`.
10. **Exact float matching.** `AVG()` results must be echoed digit for digit by the LLM. Any
    rounding fails the check.

## Documentation & housekeeping

11. **Stale or inconsistent docs.**
    - `ARCHITECTURE.md` says "not yet implemented" and refers to `offsfleet` and `images/`.
    - The README says the image directory is `images/`.
    - `REQUIREMENTS.md` checkboxes are mostly unticked even where features exist.
    - Test counts drift through the README (63 → 71 → 101 → 125).
    - `ASSIGNMENT_REVIEW.md` links to a git-ignored `chatgpt/` PDF.
12. **The README reads as a development log, not a submission guide.** It needs a short
    *Setup → Configure `.env` (`PROJECT_ID`, `GEMINI_API_KEY`) → `gcloud auth application-default login`
    → copy grants → run* section with one example session. The requirement-by-requirement
    explanation should be consolidated in one place.
13. **Minor code tidy-ups.**
    - `src/application.py` (`build_count_graph`) is legacy; only `COUNT_QUESTION` is still used.
    - `gemini.py` reads unused env vars and calls `load_dotenv` at import time.
    - The graph is recompiled on every turn.
    - `.gitignore` lists `chatgpt/` twice.
    - The `pyproject.toml` description is a placeholder.
    - Every report title is "Retail analysis — N section(s)", so `/reports` lists are hard to tell apart.

## Design-only items still to document in the HLD

These are design-only for the prototype but need a written design:

- the Golden Trio lifecycle
- user preference memory
- system learning loop
- QA/evaluation plan
- runtime persona management (Req. 8)
- a GCP deployment

Diagrams exist in `diagrams/`. The written technical explanation that ties them together is
the main missing deliverable.
