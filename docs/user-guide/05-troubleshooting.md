[← 4. Examples](04-examples.md) · **5. Troubleshooting & FAQ** · [Back to README ↑](../../README.md)

# 🩺 Troubleshooting & FAQ

- [Installation problems](#installation-problems)
- [Messages during a session](#messages-during-a-session)
- [Gemini quota and slow answers](#gemini-quota-and-slow-answers)
- [Limits and budgets](#limits-and-budgets)
- [Getting help with a failed turn](#getting-help-with-a-failed-turn)
- [Tips for a smooth experience](#tips-for-a-smooth-experience)
- [FAQ](#faq)
- [Glossary](#glossary)

---

## Installation problems

| Symptom | Cause | Fix |
|---|---|---|
| `Missing in .env: … (run scripts.setup_datachatdb first)` | `make_docker_secrets` can't find a required value | Fill in `GEMINI_API_KEY` and `PROJECT_ID`, and **uncomment** `DATACHAT_DB_PASSWORD` in `.env` ([step A.2](01-installation.md#a2-create-your-env)) |
| `No Application Default Credentials at …` | You haven't signed in with gcloud yet | `gcloud auth application-default login`, then run `make_docker_secrets` again |
| `Set PROJECT_ID and GEMINI_API_KEY in .env before running.` | Local install: values missing from `.env` | Edit `.env` |
| `Access denied: check the administrator-managed grant configuration.` (at startup) | Unknown `--user`, or the grants file is missing | Use `user-a`, `user-b` or `user-c`. Local install: `cp config/grants.executives.json config/grants.local.json` |
| `Unable to complete the request (OperationalError)…` | DataChat can't reach the report database | Docker: `docker compose up -d postgres` and wait until `docker compose ps` shows *healthy*. Local: start PostgreSQL and check the `DATACHAT_DB_*` values in `.env` |
| Database password refused after you changed it in `.env` (Docker) | The database container kept the password from its **first** start | Put the original password back in `.env` and run `make_docker_secrets` again. If the database holds nothing you need, you can instead reset it with `docker compose down -v`. ⚠️ This erases all reports. |
| `403` / `Forbidden` / `query_forbidden` from BigQuery | Your account can't run jobs in `PROJECT_ID`, or the BigQuery API is disabled | Enable the BigQuery API and grant yourself **BigQuery Job User** on the project |
| `the input device is not a TTY` | You started chat mode without `docker compose run` from a real terminal | Run it from an interactive terminal, not from a script or a pipe |

---

## Messages during a session

| Message | Meaning | What to do |
|---|---|---|
| *Start with a full analytical question. Type /help for examples.* | No question in context yet, or the text isn't supported | Begin with a question such as `Show item counts by category` |
| *That request is not supported yet. Type /help for analytical examples.* | The wording isn't in the approved list | Check `/help` or the [cheat sheet](02-using-datachat.md#cheat-sheet) |
| *Use valid YYYY-MM-01 boundaries; the end month is excluded…* | Bad date range | Use first-of-month dates, with the end date after the start |
| *No publishable aggregate was returned…* | No matching data in your scope, or every group was below 5 customers | Widen the period, remove `completed items only`, or use `overall` |
| *Access denied: the identity, report, operation, or product scope is unavailable.* | Not your report or operation, the ID was mistyped, or your access changed | Check the ID with `/reports` or `/pending` |
| *… contents unavailable under current grants* | The report was created under a different product scope | Ask an administrator, or delete it if it's no longer needed |
| *No matching owned reports; nothing deleted.* | Your `/delete` selection matched nothing | Check `/reports`. Remember that `mentioning` searches for an exact phrase |
| *Targets changed; nothing deleted…* | A listed report changed after the preview | Run `/delete …` again for a new preview |
| *Confirmation expired; nothing deleted…* | More than 15 minutes passed | Run `/delete …` again |
| *Unable to complete the request (…). Try again or use /reset.* | Unexpected error | Try again. If it keeps happening, see [Getting help](#getting-help-with-a-failed-turn) |

---

## Gemini quota and slow answers

Each analytical question uses about **2 Gemini calls** (draft the query, then return the results).
Report and deletion commands use **none**.

| Message | Meaning |
|---|---|
| *Gemini's daily quota is exhausted (HTTP 429)…* | You've used up today's quota. The **free tier allows about 20 requests per day**, or roughly 10 questions. Wait for the daily reset, or enable billing on your Gemini project. |
| *Gemini's rate or quota limit was reached (HTTP 429)…* | Too many requests in a short time. Wait a minute and try again. |
| A turn takes 30 seconds or more, then fails with a Request ID | Gemini is slow or overloaded. Your previous successful question is kept, so just repeat the follow-up. |

> [!TIP]
> **Make your quota last longer:** build a report from analyses you've already run (no model call),
> use `/reports` and `/show` freely, and avoid asking the same question twice.

---

## Limits and budgets

Each turn has a fixed **budget**. When it runs out, DataChat stops cleanly instead of carrying on
and running up costs.

| Limit | Default | Option to change it |
|---|---:|---|
| Time per turn | 120 s | `--deadline-seconds` |
| Gemini attempts per turn (including retries) | 6 | `--max-model-attempts` |
| BigQuery queries per turn | 2 | `--max-queries` |
| BigQuery cost estimates (dry runs) per turn | 2 | `--max-dry-runs` |
| BigQuery bytes reserved per turn | 200 MB | `--max-query-bytes` |
| Gemini output tokens per call | 4,096 | `--max-output-tokens` |
| Bytes billed per BigQuery job | 100 MB | *fixed* |
| Rows per result | 100 | *fixed* |
| Minimum customers per displayed group | 5 | *fixed (privacy)* |
| Analyses kept for a report | 8 most recent | *fixed* |
| Reports per deletion | 100 | *fixed* |
| Deletion confirmation window | 15 min | *fixed* |

*Reading the table:* the first six limits apply to each turn and can be **lowered** for stricter cost
control. The others are built-in safety limits. Example:

```bash
docker compose run --rm datachat --chat --user user-a --max-queries 1 --deadline-seconds 90
```

---

## Getting help with a failed turn

When a turn fails, DataChat prints a **Request ID**:

```text
Unable to complete the request (TimeoutError). Try again or use /reset.
Request ID: 8b01bbb6-2f4e-4c1a-9a57-3d0e7c2b9f11
```

Give that ID to whoever supports your installation. The trace log contains timings and error
categories for every turn, and **no** questions, data or keys. Sharing the trace file is therefore
safe, as long as your organization allows sharing usage metadata. To see a summary yourself:

```bash
# Local install
uv run python -m src.trace_summary .local/traces.jsonl
grep 'REQUEST_ID' .local/traces.jsonl

# Docker
docker compose run --rm --entrypoint python datachat -m src.trace_summary .local/traces.jsonl
```

---

## Tips for a smooth experience

- 💡 **Start broad, then narrow.** Ask `Show item counts by category`, then add `completed items only`
  and a date range. Follow-ups are cheaper to type than full questions.
- 💡 **Keep the notes with the numbers.** When you quote a figure, also quote its filters and caveats,
  or quote from a report, which includes them.
- 💡 **Report as you go.** Analysis context is lost when you quit, so run `/report` before ending a
  productive session.
- 💡 **Note the Conversation ID** whenever you start a deletion you may not finish right away.
- 💡 **Use `/reset`** when switching topics, so a follow-up isn't applied to an earlier question.
- 💡 **Copy IDs; don't retype them.** IDs are 36 characters long. A typo results in *Access denied*.

---

## FAQ

<details>
<summary><b>Can I ask questions in my own words?</b></summary>

Not yet. DataChat accepts a fixed set of approved questions and follow-ups (see `/help`). This lets
every answer be checked exactly against the approved query. Wider language support is planned.
</details>

<details>
<summary><b>Why is a category missing from my results?</b></summary>

Either it's outside your scope (see [Roles](03-roles-and-security.md#the-three-executive-roles)), or it
had fewer than 5 distinct customers in the selected period and was hidden for privacy. A missing group
**doesn't** mean zero.
</details>

<details>
<summary><b>Is "item sale value" the same as revenue?</b></summary>

No. It is the sum of item sale prices. By default it includes cancelled and returned items, and no
currency or net-revenue definition has been approved. Use `completed items only` for a closer view,
and keep the caveat.
</details>

<details>
<summary><b>Can another executive see my reports?</b></summary>

No. Reports are stored with you as their owner, and the database itself (row-level security) hides
them from other identities. Database administrators can see all records.
</details>

<details>
<summary><b>Can I recover a deleted report?</b></summary>

Not through DataChat. Deletion is "soft", meaning the record stays in the database, so an
administrator may be able to help, depending on your organization's retention policy.
</details>

<details>
<summary><b>Does DataChat change any data in BigQuery?</b></summary>

No. It runs only validated, read-only `SELECT` queries on aggregates. It writes only to its own report
database.
</details>

<details>
<summary><b>Does my question get sent to Gemini?</b></summary>

Only after it has matched an approved phrase. DataChat then sends Gemini a **standardized** version of
the request, never your raw text, together with the approved schema. Unsupported or suspicious text
never leaves your machine.
</details>

<details>
<summary><b>What does <code>--golden-demo</code> do?</b></summary>

It adds a few clearly labeled **synthetic** example questions and queries to Gemini's context, to
demonstrate the "Golden Knowledge" retrieval described in the production design. It doesn't change
the validation or the numbers you get.
</details>

---

## Glossary

| Term | Meaning |
|---|---|
| **Order item** | One product line in an order. One order can contain several items. |
| **Item status** | *Complete, Shipped, Processing, Cancelled* or *Returned*. |
| **Scope / grants** | The set of products an executive is allowed to analyze. |
| **Follow-up** | A short phrase that changes one part of the previous question. |
| **Suppression** | Hiding groups with too few customers, to protect privacy. |
| **Operation ID** | The code that identifies one pending deletion. It is used to confirm or cancel. |
| **Soft deletion** | The report is hidden from you, but the database keeps the record. |
| **Request ID** | A code that identifies one turn in the trace log, for troubleshooting. |
| **Conversation ID** | A code that identifies one chat session. Use it to resume pending deletions. |

---

[← 4. Examples](04-examples.md) · **5. Troubleshooting & FAQ** · [Back to README ↑](../../README.md)
