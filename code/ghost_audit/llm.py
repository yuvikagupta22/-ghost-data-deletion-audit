"""Optional model calls with token and cost accounting. Three providers are supported:

  * gemini    - Google Gemini over its REST API (stdlib only). Key: GEMINI_API_KEY (or GOOGLE_API_KEY).
  * anthropic - Claude through the `anthropic` SDK. Key: ANTHROPIC_API_KEY or `ant auth login`.
  * ollama    - a local model served by Ollama (e.g. Gemma 4), no key, $0. Host: OLLAMA_HOST.

The provider is GHOST_AUDIT_PROVIDER when set, otherwise Gemini when a Gemini key is present, otherwise
Anthropic when the SDK is installed. Keys are read from the environment only and never logged.

The audit pipeline is deterministic and works without any model. The model is used for:
  * screenshot extraction (`--refresh-vision`), cached on disk;
  * an independent intent check over every request text (`--llm`).
Every call is counted so evaluation/usage_report.md is produced from real numbers.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = {"gemini": "gemini-3.5-flash-lite", "anthropic": "claude-opus-5-5", "ollama": "gemma4:e4b"}
# USD per million tokens (input, output); override with GHOST_AUDIT_PRICE_IN / GHOST_AUDIT_PRICE_OUT
PRICES = {"gemini-3.5-flash-lite": (0.30, 2.50), "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-pro": (1.25, 10.00), "claude-opus-5-5": (4.00, 20.00)}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if not OLLAMA_HOST.startswith("http"):
    OLLAMA_HOST = "http://" + OLLAMA_HOST

USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "by_purpose": {}}
EVAL_DIR = Path(__file__).resolve().parents[1] / "evaluation"
USAGE_FILE = EVAL_DIR / "last_run_usage.json"
REPORT_FILE = EVAL_DIR / "usage_report.md"


def _gemini_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def provider():
    p = os.environ.get("GHOST_AUDIT_PROVIDER", "").strip().lower()
    if p:
        return p
    if _gemini_key():
        return "gemini"
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return None
    return "anthropic"


def model():
    p = provider()
    return os.environ.get("GHOST_AUDIT_MODEL") or DEFAULT_MODEL.get(p or "", "")


def available():
    p = provider()
    if p == "gemini":
        return bool(_gemini_key())
    if p == "ollama":
        try:
            with urllib.request.urlopen(OLLAMA_HOST + "/api/tags", timeout=5):
                return True
        except (OSError, urllib.error.URLError):
            return False
    if p == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True
    return False


def _count(purpose, inp, out):
    USAGE["calls"] += 1
    USAGE["input_tokens"] += inp
    USAGE["output_tokens"] += out
    p = USAGE["by_purpose"].setdefault(purpose, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
    p["calls"] += 1
    p["input_tokens"] += inp
    p["output_tokens"] += out


def complete(content, max_tokens=1024, purpose="general", system=None, json_mode=False):
    """content: Anthropic-style blocks ({"type": "text"} / {"type": "image", "source": {...}})."""
    if provider() == "gemini":
        return _gemini(content, max_tokens, purpose, system, json_mode)
    if provider() == "ollama":
        return _ollama(content, max_tokens, purpose, system, json_mode)
    return _anthropic(content, max_tokens, purpose, system)


def _gemini(content, max_tokens, purpose, system, json_mode):
    parts = []
    for b in content:
        if b.get("type") == "text":
            parts.append({"text": b["text"]})
        elif b.get("type") == "image":
            src = b["source"]
            parts.append({"inline_data": {"mime_type": src["media_type"], "data": src["data"]}})
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(GEMINI_URL.format(model=model()), data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": _gemini_key()})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt * 2)
                continue
            # the error body never contains the key; it explains bad model names, quota, etc.
            raise RuntimeError(f"Gemini API error {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}")
    u = data.get("usageMetadata", {})
    # thinking tokens are billed as output
    _count(purpose, u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0))
    cands = data.get("candidates") or []
    if not cands:
        return ""
    return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))


def _ollama(content, max_tokens, purpose, system, json_mode):
    msg = {"role": "user", "content": "\n".join(b["text"] for b in content if b.get("type") == "text")}
    images = [b["source"]["data"] for b in content if b.get("type") == "image"]
    if images:
        msg["images"] = images
    body = {"model": model(), "messages": ([{"role": "system", "content": system}] if system else []) + [msg],
            "stream": False, "think": False, "options": {"temperature": 0, "num_predict": max_tokens}}
    if json_mode:
        body["format"] = "json"
    while True:
        req = urllib.request.Request(OLLAMA_HOST + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", "replace")
            # some runners (e.g. MLX) cannot enforce JSON; callers already extract JSON from plain text
            if exc.code == 501 and "format" in body:
                del body["format"]
                continue
            raise RuntimeError(f"Ollama error {exc.code}: {err[:300]}")
    _count(purpose, data.get("prompt_eval_count", 0), data.get("eval_count", 0))
    return data.get("message", {}).get("content", "")


def _anthropic(content, max_tokens, purpose, system):
    import anthropic
    client = anthropic.Anthropic()
    kwargs = dict(model=model(), max_tokens=max_tokens, messages=[{"role": "user", "content": content}],
                  output_config={"effort": "low"})
    if system:
        kwargs["system"] = system
    try:
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except anthropic.BadRequestError:
        resp = client.messages.create(**kwargs)
    _count(purpose, resp.usage.input_tokens, resp.usage.output_tokens)
    if resp.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def price():
    if provider() == "ollama":   # runs on the local machine
        return 0.0, 0.0
    pin, pout = PRICES.get(model(), (None, None))
    pin = float(os.environ.get("GHOST_AUDIT_PRICE_IN", pin if pin is not None else 0))
    pout = float(os.environ.get("GHOST_AUDIT_PRICE_OUT", pout if pout is not None else 0))
    return pin, pout


def cost(inp, out):
    pin, pout = price()
    return inp / 1e6 * pin + out / 1e6 * pout


def save_usage(extra=None):
    data = dict(USAGE, provider=provider() if USAGE["calls"] else None, model=model() if USAGE["calls"] else None,
                est_cost_usd=round(cost(USAGE["input_tokens"], USAGE["output_tokens"]), 4))
    if extra:
        data.update(extra)
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def write_usage_report(data):
    """Render evaluation/usage_report.md from the full-dataset run that produced output.csv."""
    n = data.get("requests") or 1
    tot = data["input_tokens"] + data["output_tokens"]
    calls = data["calls"]
    prov = {"gemini": "Google", "anthropic": "Anthropic", "ollama": "Local (Ollama)"}.get(data.get("provider"), "—")
    name = f"`{data['model']}`" if data.get("model") else "none (no model configured)"
    pin, pout = price() if calls else (0, 0)
    rows = []
    for purpose, u in sorted(data["by_purpose"].items()):
        t = u["input_tokens"] + u["output_tokens"]
        rows.append(f"| {purpose} | {u['calls']} | {u['input_tokens']:,} | {u['output_tokens']:,} | {t:,} | "
                    f"${cost(u['input_tokens'], u['output_tokens']):.4f} |")
    purpose_table = "\n".join(rows) if rows else "| — | 0 | 0 | 0 | 0 | $0.0000 |"
    intent = data.get("intent_check")
    if intent:
        intent_text = (f"The model labelled {intent['labelled']} of {intent['texts']} request texts. It agreed with the "
                       f"multilingual rules on {intent['agree']} of them ({intent['agree_rate']:.1%}). "
                       f"Disagreements: {intent['disagree']} (listed in `last_run_usage.json`; the rules decide "
                       f"confident texts, the model decides the rest).")
    else:
        intent_text = "Not run (`--llm` not passed or no model configured): the multilingual rules decided every text."
    try:
        note = json.loads((Path(__file__).resolve().parent / "vision_cache" / "image_records.json")
                          .read_text(encoding="utf-8")).get("_note", "")
    except (OSError, ValueError):
        note = ""
    vision_text = ("Re-extracted in this run (see the `vision` row above)." if "vision" in data["by_purpose"]
                   else f"Not re-extracted in this run; the cached facts were used. Cache provenance: {note or 'unknown'}")
    text = f"""# Usage Report — final full-dataset run

This file is written by `python code/main.py` at the end of every full run over
`dataset/deletion_requests.csv`, from the counters in `code/evaluation/last_run_usage.json`.

## Final run that produced `output.csv`

Command: `{data.get('command', 'python code/main.py')}` · {data['requests']} requests · {data.get('seconds', '?')} s

| Provider | Model | Model calls | Input tokens | Output tokens | Total tokens | Avg tokens / request | Est. cost | Est. cost / request |
|---|---|---|---|---|---|---|---|---|
| {prov} | {name} | {calls} | {data['input_tokens']:,} | {data['output_tokens']:,} | {tot:,} | {tot / n:,.1f} | ${data['est_cost_usd']:.4f} | ${data['est_cost_usd'] / n:.6f} |
| **Overall** | | **{calls}** | **{data['input_tokens']:,}** | **{data['output_tokens']:,}** | **{tot:,}** | **{tot / n:,.1f}** | **${data['est_cost_usd']:.4f}** | **${data['est_cost_usd'] / n:.6f}** |

Prices used: ${pin:.2f} / ${pout:.2f} per million input / output tokens (`PRICES` in `code/ghost_audit/llm.py`,
override with `GHOST_AUDIT_PRICE_IN` / `GHOST_AUDIT_PRICE_OUT`). Check them against the provider's current price list.

### By purpose

| Purpose | Calls | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|
{purpose_table}

### Intent cross-check

{intent_text}

### Screenshot facts

{vision_text}

## What the model is used for

- **Request intent (`--llm`):** every request text is sent to the model in batches of 40, at temperature 0
  (prompt: `LLM_SYSTEM` / `BATCH_PROMPT` in `code/ghost_audit/intent.py`). Nothing is cached, so these counts are
  the calls this run really made.
- **Screenshots (`--refresh-vision`):** one call per image in `dataset/evidence/images/`. The facts are cached in
  `code/ghost_audit/vision_cache/image_records.json`, with a readable copy in `image_records.md`.
- **Everything else** is plain Python: identity resolution, evidence parsing, state at `audit_at`, deletion-job
  timing, policy and step ordering.

Without a key or with no flags, the run is fully deterministic and makes 0 calls.
"""
    REPORT_FILE.write_text(text, encoding="utf-8")
