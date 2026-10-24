# Usage Report — final full-dataset run

This file is written by `python code/main.py` at the end of every full run over
`dataset/deletion_requests.csv`, from the counters in `code/evaluation/last_run_usage.json`.

## Final run that produced `output.csv`

Command: `GHOST_AUDIT_PROVIDER=ollama python code/main.py --llm` · 518 requests · 17.7 s

| Provider | Model | Model calls | Input tokens | Output tokens | Total tokens | Avg tokens / request | Est. cost | Est. cost / request |
|---|---|---|---|---|---|---|---|---|
| Local (Ollama) | `gemma4:e4b` | 10 | 16,684 | 3,355 | 20,039 | 38.7 | $0.0000 | $0.000000 |
| **Overall** | | **10** | **16,684** | **3,355** | **20,039** | **38.7** | **$0.0000** | **$0.000000** |

Prices used: $0.00 / $0.00 per million input / output tokens (`PRICES` in `code/ghost_audit/llm.py`,
override with `GHOST_AUDIT_PRICE_IN` / `GHOST_AUDIT_PRICE_OUT`). Check them against the provider's current price list.

### By purpose

| Purpose | Calls | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|
| intent | 10 | 16,684 | 3,355 | 20,039 | $0.0000 |

### Intent cross-check

The model labelled 375 of 375 request texts. It agreed with the multilingual rules on 375 of them (100.0%). Disagreements: 0 (listed in `last_run_usage.json`; the rules decide confident texts, the model decides the rest).

### Screenshot facts

Not re-extracted in this run; the cached facts were used. Cache provenance: Facts transcribed from dataset/evidence/images/*.png by a vision model (claude-opus-5-5) during development. Regenerate with: python code/main.py --refresh-vision

## What the model is used for

- **Request intent (`--llm`):** every request text is sent to the model in batches of 40, at temperature 0
  (prompt: `LLM_SYSTEM` / `BATCH_PROMPT` in `code/ghost_audit/intent.py`). Nothing is cached, so these counts are
  the calls this run really made.
- **Screenshots (`--refresh-vision`):** one call per image in `dataset/evidence/images/`. The facts are cached in
  `code/ghost_audit/vision_cache/image_records.json`, with a readable copy in `image_records.md`.
- **Everything else** is plain Python: identity resolution, evidence parsing, state at `audit_at`, deletion-job
  timing, policy and step ordering.

Without a key or with no flags, the run is fully deterministic and makes 0 calls.
