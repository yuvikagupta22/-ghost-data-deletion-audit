# Evaluation Report — Ghost Data Still Exists

All numbers below come from code in this folder and can be reproduced from the repository root:

```bash
python code/evaluation/main.py --verbose        # runs the agent on the 32 public samples and scores it
python code/evaluation/custom_cases.py          # 7 hand-built cases
python code/evaluation/validate_output.py       # contract checks on the final output.csv
```

## 1. Labelled sample set (32 public cases, `dataset/sample_output.csv`)

| Metric | Definition | Score |
|---|---|---|
| Case-level status accuracy | `deletion_status` matches | **1.000** (32/32) |
| Step precision | predicted steps that exactly match a gold step (phase, system, resource, operation, state, evidence) | **0.995** |
| Step recall | gold steps that were produced | **0.979** |
| Step F1 | harmonic mean | **0.987** |
| Core step F1 | same, ignoring `evidence_id` | **0.987** |
| Field-level accuracy | steps aligned by position, share of the 7 fields that match (over the longer list) | **0.972** |
| Sequence accuracy | the entire `steps_json` array is identical | **0.906** (29/32) |

Status breakdown: gold 10 COMPLETE / 22 INCOMPLETE; predicted 10 / 22, all on the correct cases.
No predicted finding cites evidence captured after `audit_at` (enforced by `validate_output.py`).

The sample labels are used only for scoring. The deletion-job cadences behind steps 8 and 9 in §4 are learned
from the participant data without labels, so these numbers are still out-of-sample for that feature.

### Remaining sample errors (3 cases, all inside VERIFICATION)

| Cause | Cases | Why it is hard |
|---|---|---|
| Account and analytics record never tied to the person | 04 | `account_004` appears only in a replica snapshot, with no identifier and no deletion-worker activity. `analytics_901` was anonymized by a one-off worker message ("reported the requested operation as completed"), and there are too few of those to learn a cadence. |
| Retained audit record absent from all participant files | 18, 20 | `audit_record_6645` and `audit_record_5569` do not appear in any participant-visible file. |

## 2. Full evaluation run (518 cases, `output.csv`)

| | Count |
|---|---|
| INCOMPLETE | 357 |
| COMPLETE | 151 |
| NOT_APPLICABLE | 10 (exactly the 10 requests that the privacy-intake activity stream also classified as non-deletion) |
| Anonymized analytics records linked through their deletion job | 115 actions (107 new verification steps; 90+ in COMPLETE cases) |
| Accounts re-assigned by job cadence | 98 cases. 67 are independently confirmed by the case's own identifier-linked policy actions; 24 are knock-on changes in cases with no timing evidence either way; 6 are unsupported; 1 may be a regression (the old account was on-cadence). |
| Contract problems found by `validate_output.py` | 0 |
| Runtime | ≈2 s, no model calls (see `usage_report.md`) |

Spot checks against raw evidence (e.g. `case_488`, `case_34`, `case_55`, `case_220`) confirmed the linked
resources, states and cited files by hand.

## 3. Additional evaluation cases created by the team

`custom_cases.py` builds small synthetic worlds and runs them through the real `CaseAudit` and planner code.

| # | Case | What it tests | Expected | Result |
|---|---|---|---|---|
| 1 | `historical_email_and_lookalike_decoy` | Request arrives with an old email from `user_audit_log`; a `old.person.jr@…` record belongs to someone else | INCOMPLETE: only `search_9002` reported; decoy `search_9003` ignored | PASS |
| 2 | `evidence_after_audit_is_ignored` | A capture after `audit_at` shows the profile again | COMPLETE, citing only the pre-audit capture | PASS |
| 3 | `flows_disabled_before_dependent_records` | Active sync, restorable backup, identifiable analytics, shared support thread, exposed tombstone | Remediation order DISABLE → BLOCK → REDACT → ANONYMIZE; tombstone REDACT, not DELETE | PASS |
| 4 | `non_deletion_request_in_spanish` | "Quisiera recibir una copia de mis datos…" | NOT_APPLICABLE with `[]` | PASS |
| 5 | `workflow_claim_does_not_prove_deletion` | Activity says SUCCESS but a later snapshot still has the profile | INCOMPLETE with a remaining-record finding | PASS |

| 6 | `deletion_job_timing_links_anonymized_analytics` | An anonymized analytics profile with no identifiers; four deletion jobs teach the cadence; an off-cadence decoy anonymization sits in the same window | COMPLETE: `VERIFY_RECORD_ANONYMIZED` for the job's profile only | PASS |
| 7 | `lapsed_retention_is_no_longer_retained` | A security audit record whose stated `retention_until` ended before `audit_at` | INCOMPLETE: the record is reported and deleted, not retained | PASS |

**7 / 7 pass.** Cases 6 and 7 fail when their feature is switched off, so they test the new code rather than passing by default.

## 4. Failures found and improvements made

Each change was measured on the 32 labelled samples (status accuracy / step F1 / sequence accuracy):

| Step | Problem found | Change | Status | Step F1 | Seq. |
|---|---|---|---|---|---|
| 0 | First end-to-end version | — | 0.938 | 0.847 | 0.344 |
| 1 | A legal-hold PDF listing many `user_…` ids linked one tombstone to five people | Document aliases that are user ids are no longer used. A multi-resource document gives each resource only the identifiers printed on its own line. | | | |
| 2 | An empty scoped inventory snapshot "proved" profiles absent | Absence is inferred only from full primary/replica database captures, never from scoped inventories | | | |
| 3 | Retained audit records seen only in files captured after `audit_at` were dropped | Identity links may come from any capture, but **state** is read only from evidence visible at `audit_at`. Unseen records still get their required post-remediation check. | 1.000 | 0.902 | 0.438 |
| 4 | Same person with two requests (Feb and Sep) | Each record is given to the request whose audit window it was first seen in | | | |
| 5 | A read-replica captured before the request hid a later security record | SQL absence counts only for snapshots taken between `requested_at` and `audit_at`, and never for retained categories | 1.000 | 0.925 | 0.562 |
| 6 | Accounts in the same deletion batch were swapped | Activity matching also checks that the job result agrees with the reported status (SUCCESS↔COMPLETE, PARTIAL↔PARTIALLY_COMPLETE, FAILED↔NOT_STARTED), as a 30-minute penalty rather than a hard rule | 1.000 | 0.950 | 0.594 |
| 7 | Deleted profile with no lookup but a visible tombstone | A visible deleted-account tombstone for the subject is accepted as proof that the profile is absent | 1.000 | 0.955 | 0.656 |
| 8 | Accounts in one batch still swapped (cases 23, 28/29) | The deletion worker runs each subject's job at a fixed cadence: (policy-action time − account-deletion time) mod 60 s is constant per action kind (e.g. completed support redaction: 10 s; data-flow disable: 39–40 s). The cadence is learned from identifier-linked pairs. A candidate account whose deletion is on-cadence with the subject's own linked policy actions is now preferred over the closest-`reported_at` guess | 1.000 | 0.971 | 0.719 |
| 9 | Anonymized analytics never linked (cases 03, 21, 25–28) | Such profiles have no identifiers, so no labelled pairs exist. Their cadence is learned **without labels**: each action is paired with every account deletion in a 6 h window, and the true residue forms a sharp peak (41/40 s: 132 hits against a background of about 7.5 per residue). It is accepted only if it is ≥ 4× the mean and ≥ 1.5× the best residue elsewhere. An action no identifier ties to anyone is given to the one case whose account job it sits on; ambiguous matches are skipped | 1.000 | **0.987** | **0.906** |
| 10 | `deletion_rules.csv` conditions were ignored | RETAIN rules with a time-bounded condition (security audit retention period, active legal hold) now check the record's own `retention_until` / `retained_until`; a lapsed record is deleted. No record in this dataset has lapsed, so scores are unchanged | 1.000 | 0.987 | 0.906 |
| 11 | No real model in the run | `--llm` labels every request text with Gemini, Claude or a local Ollama model such as Gemma 4 (batched, temperature 0) as an independent cross-check of the rules; `--refresh-vision` re-extracts screenshots through the same metered client. `usage_report.md` is now generated by the run itself | – | – | – |

Other fixes along the way:

- Output was sorted as text (`case_100` before `case_33`). It is now sorted numerically, matching the input file.
- The intent rules first matched "copy of my profile" as an access request. Negation phrases were tightened and now decide all 550 public request texts confidently.

## 5. Model choice: what the models were used for, and what they were not

Both models tried were measured against the pipeline itself. The sample answers were not involved.

| Task | Gemini `gemini-3.5-flash-lite` (API) | Gemma 4 `gemma4:e4b` (local, Ollama) | Decision |
|---|---|---|---|
| Request intent, all 375 distinct texts | 375/375 agree with the rules (10 calls) | 375/375 agree with the rules (10 calls, 36 s, $0) | Model labels every text as a cross-check in the final run (Gemma). The rules decide confident texts, the model decides the rest. |
| Screenshot extraction (11 images, 22 real resource ids) | Used `user_20` instead of `analytics_7438`, found 1 of 6 audit records, invented `object_1…8`, returned `text` as a list | Missed 7 of 22 ids (incl. `account_5762`, `marketing_001`, 5 audit records), invented 39 | **Kept the verified cache.** With Gemini's extraction, cases 50 and 53 wrongly flipped to COMPLETE, because an exposed analytics record disappeared. |

`gemini-2.5-flash` is no longer available to new keys, and the larger `gemini-3.x-flash` models took about 80 s per call
under load (503 "high demand"), so Flash-Lite was the practical Gemini choice. The parser now accepts a list-valued
`text` field from any model.

## 6. Known limitations

- Deletion-job linkage relies on the worker's fixed cadence. If jobs overlapped so that two cases matched one action, the action is skipped rather than guessed. This affects verification steps only, never the status.
- Account selection from the activity stream is still a heuristic when the subject has no identifier-linked policy actions to compare.
- A linked record with no state visible at `audit_at` is left out of a COMPLETE case's checks, because they must cite evidence; an INCOMPLETE case still gets its post-remediation check.
- The intent rules were tuned on the 550 public request texts. Phrases like "keep my account" override deletion words, so `--llm` is the safeguard: it reports every text where the model and the rules disagree.
- The screenshot facts are a cached transcription (see `usage_report.md`). If the images change, re-run `--refresh-vision`.
