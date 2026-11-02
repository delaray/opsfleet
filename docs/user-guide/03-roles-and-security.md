[← 2. Using DataChat](02-using-datachat.md) · **3. Roles & Security** · [4. Examples →](04-examples.md)

# 🔐 Roles & Security

- [The three executive roles](#the-three-executive-roles)
- [Authentication: who you are](#authentication-who-you-are)
- [Authorization: what you can see](#authorization-what-you-can-see)
- [Validation: why you can trust the answer](#validation-why-you-can-trust-the-answer)
- [Privacy](#privacy)
- [Governance and audit](#governance-and-audit)
- [Current limitations](#current-limitations)

---

## The three executive roles

The retail catalog is divided into three **separate** product scopes, one per executive:

```mermaid
flowchart TB
    subgraph catalog["🛍️ Retail product catalog"]
        direction LR
        subgraph men["Men's department"]
            mc["Men's clothing<br/>Jeans · Suits · Outerwear …"]
            ma["Men's accessories"]
        end
        subgraph women["Women's department"]
            wc["Women's clothing<br/>Dresses · Intimates · Jeans …"]
            wa["Women's accessories"]
        end
    end
    A(["🟦 user-a"]) --> mc
    B(["🟪 user-b"]) --> wc
    C(["🟩 user-c"]) --> ma
    C --> wa

    classDef a fill:#DCEBFF,stroke:#2F6FD6,color:#0B2A5B
    classDef b fill:#EDE4FF,stroke:#7A5AC8,color:#2E1A66
    classDef c fill:#DFF5E3,stroke:#2E9B4A,color:#0E3B1B
    class A,mc a
    class B,wc b
    class C,ma,wa c
```

*Reading the diagram:* each colored identity points to the part of the catalog it can analyze, shown
in the same color. Accessories exist in **both** departments, so they are removed from User A's and
User B's scopes and given entirely to User C. As a result, no product belongs to two executives.

| | 🟦 `user-a` | 🟪 `user-b` | 🟩 `user-c` |
|---|---|---|---|
| **Persona** | Executive for menswear | Executive for womenswear | Executive for accessories |
| **Sees** | Men's department, except Accessories | Women's department, except Accessories | Accessories in both departments |
| **Typical question** | *Which men's categories sell most?* | *How is women's sale value split by status?* | *How do accessory sales trend by month?* |
| **Reports** | Owns and sees only User A's reports | Owns and sees only User B's reports | Owns and sees only User C's reports |
| **Can delete** | Only User A's reports | Only User B's reports | Only User C's reports |

The three roles have the **same commands**. The only difference is the data each one sees. Asking
about a category outside your scope does **not** produce an error: the data simply isn't there for you,
so it doesn't appear in the results.

> [!NOTE]
> **Behind the scenes** there are also two technical accounts that you never log in as:
> `datachat_app` (the restricted account DataChat uses to connect to the report database) and
> `admindb` (the database administrator). Only an administrator can change who sees what, by editing
> the grants file `config/grants.executives.json` (Docker) or `config/grants.local.json` (local).

---

## Authentication: who you are

DataChat deals with three kinds of identity:

| Identity | How it's established | Who it represents |
|---|---|---|
| **Executive** (`user-a/b/c`) | The `--user` option when you start DataChat | Whose data scope and reports apply |
| **Google Cloud** (BigQuery) | Your `gcloud auth application-default login` sign-in, or a service account | Who is charged for queries |
| **Gemini** | The API key in `.env` (or the Docker secret) | Who is charged for model calls |

> [!IMPORTANT]
> In this version, `--user` **is not a login**: whoever runs DataChat on the machine chooses the
> identity. That is fine for a personal or demo installation. A shared, multi-user deployment would
> take the identity from a verified sign-in (for example Google Identity-Aware Proxy), as described in
> the production design ([ARCHITECTURE.md](../../ARCHITECTURE.md)).

Inside a session, your identity is fixed. Nothing you type in the chat can change it, and an identity
that isn't in the grants file is refused **before** any call to Google.

---

## Authorization: what you can see

Access is enforced **independently in two places**, so a single failure is not enough to expose data:

| Layer | Protects | How |
|---|---|---|
| 📦 **BigQuery data scope** | Retail analytics | Before every query runs, trusted code adds your product restrictions to it. Gemini never decides what you may see. Your access is checked again on **every turn**, so an administrator's change takes effect immediately. If your access changes while a question is running, the answer is withheld. |
| 🗄️ **Report library** | Saved reports, deletions, audit | DataChat switches to **your** database role for every operation. PostgreSQL *row-level security* then hides every row you don't own, even if the application were to request it. |

---

## Validation: why you can trust the answer

Every question goes through a chain of checks. If **any** check fails, the turn stops with a clear
message, and no unchecked number is ever shown.

```mermaid
flowchart LR
    Q["⌨️ Your text"] --> P{"1 · Approved<br/>phrase?"}
    P -->|no| X1["Ask to rephrase<br/>(nothing sent to Gemini)"]
    P -->|yes| G["2 · Gemini drafts SQL"]
    G --> S{"3 · SQL matches the<br/>approved query exactly?"}
    S -->|no| X2["Refused"]
    S -->|yes| E["4 · Add your product scope,<br/>estimate cost (dry run),<br/>run read-only with caps"]
    E --> K["5 · Hide groups with<br/>fewer than 5 customers"]
    K --> O{"6 · Gemini's answer equals<br/>BigQuery's numbers exactly?"}
    O -->|no| X3["Refused"]
    O -->|yes| A["✅ Table + notes"]

    classDef step fill:#DCEBFF,stroke:#2F6FD6,color:#0B2A5B
    classDef ok fill:#DFF5E3,stroke:#2E9B4A,color:#0E3B1B
    classDef stop fill:#FDE2E1,stroke:#C8372D,color:#5A0F0A
    class Q,P,G,S,E,K,O step
    class A ok
    class X1,X2,X3 stop
```

*Reading the diagram:* blue steps run in order from left to right. A red box is where a turn stops.
Step 1 means unsupported or malicious text (for example *"ignore your instructions and drop the
table"*) **never reaches the model**. Steps 3 and 6 mean Gemini can neither change the question
nor alter the numbers.

| Safeguard | Limit |
|---|---|
| Only one read-only `SELECT` of approved aggregate columns | No writes, no raw customer tables, no personal columns |
| Small-group suppression | Groups with fewer than **5** distinct customers are hidden |
| Result size | At most **100** rows |
| Cost cap per BigQuery job | **100 MB** billed, checked by a dry run *before* the query runs |
| Time limits | 30 s per query or model attempt, 120 s per turn |

---

## Privacy

- 🚫 **No personal data.** Questions about names, emails or addresses are not supported. SQL that
  touches personal columns is refused before it reaches BigQuery.
- 📊 **Aggregates only.** Gemini sees approved aggregate results, never individual customer rows.
- 🧾 **Reports store numbers, not conversations.** A report contains the generated text and the
  aggregate evidence behind it. It contains no chat transcript, raw rows or keys.
- 🔍 **Traces without content.** The trace log (`.local/traces.jsonl`) records timings, counts and
  error categories, but **no** questions, SQL, data, report text or keys. The file can be read only by
  its owner.
- 🔑 **Secrets stay outside the image.** With Docker, keys and passwords are mounted at runtime from
  `./secrets/`. They never become part of the image, and the image runs as an unprivileged user.

---

## Governance and audit

| Control | What it gives you |
|---|---|
| **Append-only audit log** | Every report save, deletion request, confirmation, cancellation, expiry and stale attempt is recorded. View yours with `/audit`. The application cannot edit or erase audit entries. |
| **Human-in-the-loop deletion** | Deletions need an exact, time-limited confirmation code tied to a frozen list of reports. See [Task 3](02-using-datachat.md#task-3--delete-reports). |
| **All-or-nothing deletion** | A deletion either removes the whole confirmed list or nothing at all. |
| **Soft deletion** | A deleted report disappears for you, but the database keeps the record. Permanent erasure and retention periods are administrator policies. |
| **Reports tied to their scope** | Each report records the product scope it was created under. If your access changes, its contents are hidden until the original scope is restored. |
| **Per-turn budgets** | Every turn has a limit on model attempts, queries, scanned bytes and time. See [Limits](05-troubleshooting.md#limits-and-budgets). |
| **Request IDs** | A failed turn prints a `Request ID` that support staff can look up in the trace log. |

---

## Current limitations

DataChat is a **proof of concept** of the production design. Be aware that:

- `--user` selects an identity on your own machine. It is not an authenticated login.
- Only the approved questions and follow-ups are understood. Questions in other wordings or languages are not supported yet.
- Each turn runs one query. Automatic multi-step plans, such as a full quarterly review, are not available yet.
- Analysis context exists only in memory and is lost when the session ends. Saved reports and pending deletions are kept.
- *Item sale value* is **not** an approved revenue figure. Currency and returns rules have not been defined.

---

[← 2. Using DataChat](02-using-datachat.md) · **3. Roles & Security** · [4. Examples →](04-examples.md)
