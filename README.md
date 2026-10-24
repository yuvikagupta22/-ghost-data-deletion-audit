# HackerRank Orchestrate - October 2026

## Our solution: evidence-first deletion audit agent

### Setup and run

Python 3.10+ (tested on 3.13). From the repository root:

```bash
pip install -r code/requirements.txt          # pdfplumber (Gemini needs no extra package)
python code/main.py                           # writes ./output.csv (518 rows, ~2 s, no API key needed)
python code/evaluation/main.py --verbose      # score on the 32 labelled samples
python code/evaluation/custom_cases.py        # 7 extra hand-built cases
python code/evaluation/validate_output.py     # submission-contract checks on output.csv
```

There are two optional, model-backed flags. Keys are read from the environment only. Never put one in a file
that is committed or zipped.

| Provider | Setup | Default model |
|---|---|---|
| Google Gemini | `export GEMINI_API_KEY=...` (PowerShell: `$env:GEMINI_API_KEY="..."`) | `gemini-3.5-flash-lite` |
| Anthropic Claude | `pip install anthropic` and `ANTHROPIC_API_KEY` or `ant auth login` | `claude-opus-5-5` |
| Local model via Ollama | `ollama pull gemma4:e4b`, then `GHOST_AUDIT_PROVIDER=ollama` (no key; `OLLAMA_HOST` if not `localhost:11434`) | `gemma4:e4b` |

Gemini is used when its key is set. Force a provider with `GHOST_AUDIT_PROVIDER=gemini|anthropic|ollama`, and change the model with `GHOST_AUDIT_MODEL`.

- `python code/main.py --llm` makes the model label every request text (batches of 40). It decides the texts the rules cannot, and the run reports how often it agrees with the rules.
- `python code/main.py --refresh-vision` re-extracts the screenshot facts into `code/ghost_audit/vision_cache/` (one call per image) and writes a readable `image_records.md`.
- A full run writes `code/evaluation/usage_report.md` from the run's own token counters. The submitted `output.csv` came from `GHOST_AUDIT_PROVIDER=ollama python code/main.py --llm` (local Gemma 4). Screenshots use the verified cache, because both models we tried extracted them less accurately (see the evaluation report).

### How it works (`code/ghost_audit/`)

1. **Intent** (`intent.py`): multilingual rules decide whether the text authorises deletion. Requests for access, export, correction or marketing opt-out become `NOT_APPLICABLE`.
2. **Identity** (`audit.py`): the request email (current or historical) resolves to a `user_id`. The agent then gathers every email, SHA-256 email hash, phone and the `user_id` from `user_identities.csv` and `user_audit_log.csv`. Matching is exact, so look-alike decoys never match.
3. **Evidence** (`evidence.py`, `vision.py`): every file becomes an *observation* with a resource, system, time, state and identifiers. Supported inputs:
   - JSON bundles and exports;
   - the text handover;
   - 9 log syntaxes, where a trace's lookup key is joined to its result line;
   - SQLite snapshots with primary and replica tables;
   - PDF text;
   - cached screenshot facts.

   Verification activities (`CHECK_DATA_FLOW`, `CHECK_RESTORE_ACCESS`, `RESTORE_BACKUP`) also count as observations. Workflow results (`SUCCESS`, `FAILED`) are treated as claims only.
4. **Discovery:** resources link to the person through:
   - identifiers in any capture;
   - data-catalog `LINK_RESOURCE_IDENTITY` events;
   - resource references, such as a search document pointing to its source account or a restore that repopulated a copy;
   - as a last resort, the deletion workflow's own account activity, matched by time and by reported-status consistency.

   - **deletion-job timing:** the deletion worker runs one subject's policy actions after its account deletion at a fixed cadence, so (action time − account-deletion time) mod 60 s is constant per action kind. The cadence is learned from identifier-linked pairs, or without labels from a sharp residue peak when none exist (anonymized analytics have no identifiers). It picks the right account from a deletion batch and links anonymized records to the one job they belong to.

   When one person filed several requests, each record goes to the request in whose window it was first seen.
5. **State at `audit_at`:** the agent takes the latest observation from evidence captured at or before `audit_at`. Full database captures taken after the request also prove absence.
6. **Policy and plan** (`planner.py`): each resource maps to a `deletion_rules.csv` category: DELETE, REDACT, ANONYMIZE, RETAIN, tombstone REDACT, disable the data flow, or block the restore. Time-bounded RETAIN conditions (retention period, active legal hold) are checked against the record's own stated end date. The case is COMPLETE only if every resource already has its required state. Steps are ordered by phase, then by operation in the specified order, then by `system_id` and `resource_id`. Findings and COMPLETE checks cite the observation that proves them; future steps carry `""`.

Reports: `code/evaluation/evaluation_report.md` and `code/evaluation/usage_report.md`.

---

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

Build your solution in `code/main.py`, or use another language and document its setup and run command clearly in your README.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the starter Python entry point with:

```bash
python3 code/main.py
```

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
code/evaluation/evaluation_report.md
code/evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` inside `code/evaluation/` (matching `code/evaluation/main.py` in this starter layout), write your complete evaluation metrics, custom evaluation test cases, and token/cost breakdowns directly inside those Markdown files, and keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, code -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in `code/evaluation/` so they are bundled automatically with `code` (or add `evaluation` to the archive command above if you placed them in a top-level `evaluation/` directory).
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `code/evaluation/evaluation_report.md`, and `code/evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
