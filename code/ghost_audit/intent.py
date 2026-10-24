"""Does the request text authorise deletion?

Multilingual rules decide almost every request. With `--llm` the model (Gemini or Claude, see llm.py)
independently labels every request text: it decides the texts the rules cannot, and its agreement with the
rules is reported as a cross-check. Without a model, the rule score decides.
"""
import json
import re

from . import llm

# strong deletion vocabulary (en, es, pt, fr, de, it, ja, ko, zh)
DELETE_PATTERNS = [
    r"\bdelet", r"\beras", r"\bpermanent(ly)? (account )?(delet|remov|clos)", r"\bclose(d)? (my|this|the) account",
    r"\b(would like|want) it closed", r"\bclose the account", r"\bclose (it|everything)",
    r"\bremove (my|the|this) (account|profile|personal)", r"\bremove .{0,40}(profile|account)",
    r"\b(account|profile).{0,60}\bremov", r"\bdisconnected my account",
    r"elimin", r"exclus", r"exclu[ií]", r"suppr", r"l[öo]sch", r"cancell", r"削除", r"삭제", r"删除",
    r"\bforget me\b", r"right to be forgotten", r"\bdeletion\b", r"\bremov", r"\b(is|are) gone\b",
]
# phrases that say the customer is NOT asking for deletion; these override deletion words
OTHER_PATTERNS = [
    r"\b(send me|need) a copy\b", r"\bdownload my\b", r"\bportable format\b", r"\bwhat personal information\b",
    r"\bmarketing lists only\b", r"\bstop (the )?promotional\b", r"\bunsubscribe\b", r"\bopt[- ]?out\b",
    r"\bmisspelled\b", r"\bout of date\b", r"\bchange the email\b", r"\breset(ting)? the login\b",
    r"\bleave my profile\b", r"\bdo not (delete|close|remove)\b", r"\bdon'?t delete\b", r"\bkeep (my|the) account\b",
    r"\bnot (asking for|a request to|requesting) (deletion|erase|delete)", r"\bwithout removing\b",
    r"\bstill (need|want|use) (the|my|it)\b", r"\bcontinue using\b", r"\bleave the account active\b",
    r"\binformation request\b",
]


def score(text):
    t = (text or "").lower().replace("’", "'")
    pos = sum(1 for p in DELETE_PATTERNS if re.search(p, t))
    neg = sum(1 for p in OTHER_PATTERNS if re.search(p, t))
    return pos, neg


def rule_decision(text):
    """Return (authorises_deletion: bool, confident: bool)."""
    pos, neg = score(text)
    if neg:
        return False, True
    if pos:
        return True, True
    return False, False


LLM_SYSTEM = ("You classify customer privacy requests. Answer with exactly one word: DELETE if the "
              "customer asks to delete, erase or close their account or personal data (including asking "
              "to verify or finish an earlier deletion), otherwise OTHER (access, export, correction, "
              "marketing opt-out, login help, etc.).")


BATCH_SYSTEM = ("You classify customer privacy requests, written in any language. DELETE means the customer asks "
                "to delete, erase or close their account or personal data (including asking to verify or finish an "
                "earlier deletion). OTHER means anything else: access, export or a copy of data, correction, "
                "marketing opt-out, login help, or a request that explicitly says not to delete.")
BATCH_PROMPT = ("Classify each request below. Return ONLY a JSON object mapping every id to \"DELETE\" or "
                 "\"OTHER\", for example {\"r0\": \"DELETE\", \"r1\": \"OTHER\"}.\n\n")
BATCH_SIZE = 40

LLM_LABELS = {}   # request text -> True (deletion) / False, from classify_all()


def classify_all(texts):
    """Label every distinct text with the model, in batches. Returns a cross-check summary of model
    labels vs the rules. Rules still decide the texts they are confident about (see authorises_deletion)."""
    uniq = sorted(set(texts))
    for i in range(0, len(uniq), BATCH_SIZE):
        chunk = uniq[i:i + BATCH_SIZE]
        prompt = BATCH_PROMPT + "\n".join(f"r{j}: {json.dumps(t, ensure_ascii=False)}" for j, t in enumerate(chunk))
        answer = llm.complete([{"type": "text", "text": prompt}], max_tokens=64 * len(chunk) + 2048,
                              purpose="intent", system=BATCH_SYSTEM, json_mode=True)
        m = re.search(r"\{.*\}", answer or "", re.S)
        try:
            labels = json.loads(m.group(0)) if m else {}
        except ValueError:
            labels = {}
        for j, t in enumerate(chunk):
            v = str(labels.get(f"r{j}", "")).upper()
            if v in ("DELETE", "OTHER"):
                LLM_LABELS[t] = v == "DELETE"
    labelled = [t for t in uniq if t in LLM_LABELS]
    disagree = [{"text": t[:160], "rules": rule_decision(t)[0], "model": LLM_LABELS[t]}
                for t in labelled if rule_decision(t)[0] != LLM_LABELS[t]]
    n = len(labelled)
    return {"texts": len(uniq), "labelled": n, "agree": n - len(disagree),
            "agree_rate": (n - len(disagree)) / n if n else 0.0, "disagree": len(disagree),
            "disagreements": disagree}


def authorises_deletion(text, use_llm=False):
    decision, confident = rule_decision(text)
    if confident:
        return decision
    if text in LLM_LABELS:
        return LLM_LABELS[text]
    if not use_llm or not llm.available():
        return decision
    answer = llm.complete([{"type": "text", "text": text}], max_tokens=256, purpose="intent", system=LLM_SYSTEM)
    if "DELETE" in (answer or "").upper():
        return True
    if "OTHER" in (answer or "").upper():
        return False
    return decision
