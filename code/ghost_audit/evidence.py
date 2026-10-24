"""Turn every participant-visible evidence file into normalised observations.

An Observation says: "at time `ts`, evidence `evidence_id` saw resource `resource_id`
(in `system`) in state `state`, and the record carried these identifier `keys`".

Supported inputs: JSON records, plain-text handovers, 9 log syntaxes, SQLite SQL
snapshots, PDF text, and screenshot facts extracted once by a vision model (see vision.py).
"""
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .common import (DATASET, RESOURCE_RE, identifier_keys, keys_of, parse_ts, prefix_of,
                     read_csv, system_of)
from . import vision

STATES = {"PRESENT", "ABSENT", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED"}
RESULT_MAP = {"FOUND": "PRESENT", "NOT_FOUND": "ABSENT", "HIT": "PRESENT", "MISS": "ABSENT"}

# fields whose non-null value means a direct identifier is still exposed
DIRECT_FIELDS = {"email", "phone", "email_hash", "full_name", "name", "display_name", "requester_email",
                 "requester_name", "requester_phone", "owner_email", "owner_phone", "customer_email",
                 "uploaded_by", "matched_value"}
# trace-level keys that say who a log trace was about
TRACE_KEY_FIELDS = {"lookup_key", "match_key", "scan_key", "subject_key", "owner_key", "subject_ref",
                    "subject_reference", "correlation_key", "lookup_value"}


@dataclass
class Observation:
    evidence_id: str
    file_time: object
    ts: object
    system: str
    resource_id: str
    state: str = None
    keys: set = field(default_factory=set)
    hint: str = ""          # record type / event / collection, used to classify the resource
    source: str = "file"    # file | activity | sql_absence
    retain_until: object = None   # end of a stated retention period / legal hold, if the record shows one


def _null(v):
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "-", "—"))


def _retain_until(f):
    for k in ("retention_until", "retained_until", "retain_until", "hold_until"):
        t = parse_ts(f.get(k)) if isinstance(f.get(k), str) else None
        if t:
            return t
    return None


def _true(v):
    return str(v).strip().lower() in ("true", "1", "yes", "allow", "allowed")


def _false(v):
    return str(v).strip().lower() in ("false", "0", "no", "deny", "denied")


def derive_state(system, f, hint=""):
    """Infer the resource state from one record's fields."""
    hint_l = hint.lower()
    res = str(f.get("result", "")).strip().upper()
    if res in STATES:
        return res
    if res in RESULT_MAP:
        return RESULT_MAP[res]
    if system == "integration_service" and "enabled" in f:
        return "ACTIVE" if _true(f["enabled"]) else "DISABLED"
    if system == "backup_service":
        if "restore_allowed" in f:
            return "ACTIVE" if _true(f["restore_allowed"]) else "BLOCKED"
        if "deletion_block" in f:
            return "BLOCKED" if _true(f["deletion_block"]) or _false(f.get("eligible", "true")) else "ACTIVE"
        if "decision" in f:
            return "BLOCKED" if _false(f["decision"]) else "ACTIVE"
    for k in ("http_status", "status_code"):
        if str(f.get(k, "")) == "404":
            return "ABSENT"
    if str(f.get("rows", "")) == "0" or str(f.get("total_hits", "")) == "0":
        return "ABSENT"
    hits = f.get("hits")
    if isinstance(hits, dict) and str(hits.get("total", "")) == "0":
        return "ABSENT"
    for k in ("available", "download_available"):
        if k in f and _false(f[k]):
            return "ABSENT"
    if str(f.get("account_status", "")).upper() == "DELETED" and system == "account_service":
        return "ABSENT"

    exposed = any(not _null(f.get(k)) for k in DIRECT_FIELDS if k in f)
    if system == "analytics_platform":
        if "contact_lookup" in hint_l:
            return "PRESENT"
        if "direct_identifiers" in f:
            return "ANONYMIZED" if str(f["direct_identifiers"]) == "0" else "IDENTIFIABLE"
        attributable = exposed or any(not _null(f.get(k)) for k in ("user_id", "customer_reference") if k in f)
        return "IDENTIFIABLE" if attributable else "ANONYMIZED"
    if system == "support_platform":
        if "attachment" in hint_l or "file" in f or "file_name" in f or "contact_query" in hint_l \
                or "deletion_marker" in hint_l:
            return "PRESENT"
        if "message_text" in f or "message_excerpt" in f:
            text = str(f.get("message_text", f.get("message_excerpt", "")))
            if "[REDACTED]" in text.upper() and not exposed:
                return "REDACTED"
            if exposed or keys_of(text):
                return "IDENTIFIABLE"
            return "REDACTED"
        if "pii_scan" in hint_l:
            return "IDENTIFIABLE"
        return "IDENTIFIABLE" if exposed else "REDACTED"
    if system == "identity_service":
        return "IDENTIFIABLE" if exposed else "REDACTED"
    if system == "email_delivery_service":
        return "IDENTIFIABLE" if exposed else "REDACTED"
    return "PRESENT"


# ---------------------------------------------------------------- JSON


def _json_records(obj, ctx, out):
    """Find every dict carrying a resource_id; inherit system/time from ancestors."""
    if isinstance(obj, dict):
        ctx = dict(ctx)
        for k in ("data_store", "system", "origin_system"):
            if isinstance(obj.get(k), str):
                ctx["system"] = obj[k]
        for k in ("captured_at", "observed_at", "recorded_at", "exported_at"):
            if isinstance(obj.get(k), str) and parse_ts(obj[k]):
                ctx["ts"] = obj[k]
        for k in ("collection", "record_type"):
            if isinstance(obj.get(k), str):
                ctx["hint"] = obj[k]
        if isinstance(obj.get("resource_id"), str):
            out.append((obj, ctx))
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                sub = dict(ctx)
                if not isinstance(obj.get("resource_id"), str):
                    sub["hint"] = ctx.get("hint", "") + "/" + k
                _json_records(v, sub, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_records(v, ctx, out)


def parse_json(path, eid, ftime):
    data = json.loads(path.read_text(encoding="utf-8"))
    found = []
    _json_records(data, {}, found)
    obs = []
    for rec, ctx in found:
        rid = rec["resource_id"]
        system = system_of(rid) if system_of(rid) else ctx.get("system")
        if ctx.get("system") in (system_of(rid),):
            system = ctx["system"]
        hint = str(rec.get("record_type") or ctx.get("hint", ""))
        flat = {k: v for k, v in rec.items()}
        if isinstance(rec.get("profile"), dict):
            flat.update(rec["profile"])
        elif "profile" in rec and rec["profile"] is None and str(rec.get("status_code")) != "200":
            pass
        keys = keys_of(rec)
        for v in rec.values():
            if isinstance(v, str) and RESOURCE_RE.fullmatch(v) and v != rid:
                keys.add("res:" + v)
        obs.append(Observation(eid, ftime, parse_ts(ctx.get("ts")) or ftime, system, rid,
                               derive_state(system, flat, hint), keys, hint, retain_until=_retain_until(flat)))
    return obs


# ---------------------------------------------------------------- text handover


def parse_text(path, eid, ftime):
    obs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) >= 5 and RESOURCE_RE.fullmatch(parts[2] or ""):
            ts, system, rid, rtype, result = parts[:5]
            value = parts[5] if len(parts) > 5 else ""
            f = {"result": result, "record_type": rtype}
            obs.append(Observation(eid, ftime, parse_ts(ts) or ftime, system, rid,
                                   derive_state(system, f, rtype), identifier_keys(value), rtype))
    return obs


# ---------------------------------------------------------------- logs

KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def parse_log_line(line):
    ts = line[:20]
    rest = line[21:].strip()
    d = {}
    if rest.startswith("{"):
        try:
            d = json.loads(rest)
        except ValueError:
            return None
    else:
        m = re.match(r"\[(\w+)\] \[([\w.\-]+)\] \[([\w.\-]+)\] (\w+): (.*)", rest)
        if m:
            d = {"service": m[2], "trace_id": m[3], "event": m[4]}
            rest = m[5]
        m = re.match(r"\| (\w+) \| ([\w.\-]+) \| ([\w.\-]+) \| (\w+) \| (.*)", rest)
        if m:
            d = {"service": m[2], "trace_id": m[3], "event": m[4]}
            rest = m[5]
        m = re.match(r"<\d+>1 \S+ ([\w.\-]+) ([\w.\-]+) - - (.*)", rest)
        if m:
            d = {"service": m[1], "trace_id": m[2]}
            rest = m[3]
        for k, v in KV_RE.findall(rest):
            if v.startswith('"'):
                v = v[1:-1].replace('\\"', '"')
            d.setdefault(k, v)
        if "message" in d and "=" in str(d["message"]):
            for k, v in KV_RE.findall(d["message"]):
                if v.startswith('"'):
                    v = v[1:-1]
                d.setdefault(k, v)
            d["service"] = d.get("function", d.get("service"))
            d["trace_id"] = d.get("requestId", d.get("trace_id"))
    d["_ts"] = ts
    return d


def parse_log(path, eid, ftime):
    lines = [parse_log_line(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    lines = [d for d in lines if d]
    trace_keys = defaultdict(set)
    for d in lines:
        tid = d.get("trace_id")
        if tid:
            for k in TRACE_KEY_FIELDS:
                if k in d:
                    trace_keys[tid] |= identifier_keys(d[k])
    obs = []
    for d in lines:
        rid = d.get("resource_id")
        event = str(d.get("event", ""))
        if not rid or not RESOURCE_RE.fullmatch(rid) or event.endswith("_started"):
            continue
        system = d.get("system") if d.get("system") in (system_of(rid),) else system_of(rid)
        f = {k: v for k, v in d.items() if not k.startswith("_")}
        hint = (d.get("record_type") or "") + ":" + event + ":" + str(d.get("service", ""))
        keys = set()
        for k, v in f.items():
            if k in TRACE_KEY_FIELDS or k in DIRECT_FIELDS or k in ("customer_reference", "user_id"):
                keys |= identifier_keys(v)
        keys |= trace_keys.get(d.get("trace_id"), set()) if d.get("trace_id") else set()
        obs.append(Observation(eid, ftime, parse_ts(d["_ts"]) or ftime, system, rid,
                               derive_state(system, f, hint), keys, hint, retain_until=_retain_until(f)))
    return obs


# ---------------------------------------------------------------- SQL

TABLE_SYSTEM = {
    "customer_profiles": "account_service", "customer_search_documents": "search_service",
    "search_documents": "search_service", "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform", "customer_exports": "export_service",
    "connector_subscriptions": "integration_service", "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}


def parse_sql(path, eid, ftime):
    """Load a snapshot into in-memory SQLite. Returns observations and the systems it fully covers."""
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    obs, covered = [], set()
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    # only full primary/replica database captures prove absence; scoped inventories do not
    full_capture = any(t.endswith("_primary") or "_replica" in t for t in tables) and "snapshot_scope" not in tables
    for t in tables:
        cur = con.execute(f'select * from "{t}"')
        cols = [c[0] for c in cur.description]
        if t == "snapshot_scope":
            continue
        for row in cur:
            f = dict(zip(cols, row))
            if "record_json" in f:
                try:
                    f.update(json.loads(f["record_json"]))
                except ValueError:
                    pass
                f.pop("record_json", None)
            rid = f.get("resource_id")
            if not rid:
                continue
            system = f.get("origin_system") or system_of(rid)
            for base, sysname in TABLE_SYSTEM.items():
                if t.startswith(base):
                    system = system_of(rid) or sysname
            covered.add(system)
            if system == "integration_service" and "enabled" in f:
                f["enabled"] = bool(f["enabled"])
            if system == "backup_service" and "restore_allowed" in f:
                f["restore_allowed"] = bool(f["restore_allowed"])
            hint = f"{t}:{f.get('collection_name', '')}"
            keys = keys_of({k: v for k, v in f.items() if k not in ("updated_at",)})
            for k, v in f.items():
                if isinstance(v, str) and RESOURCE_RE.fullmatch(v) and v != rid:
                    keys.add("res:" + v)
            obs.append(Observation(eid, ftime, ftime, system, rid, derive_state(system, f, hint), keys, hint,
                                   retain_until=_retain_until(f)))
    con.close()
    return obs, (covered if full_capture else set())


# ---------------------------------------------------------------- PDF


def pdf_text(path):
    try:
        import pdfplumber
    except ImportError:
        return ""
    with pdfplumber.open(str(path)) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def parse_document_text(text, eid, ftime, aliases, hint):
    """Business documents (PDF / screenshots): every referenced resource is shown as present."""
    obs = []

    def text_keys(t):
        keys = set()
        for tok in re.split(r"[\s|,;()]+", t):
            keys |= identifier_keys(tok.strip(".:"))
        return keys

    rids = {m.group(0) for m in RESOURCE_RE.finditer(text)}
    for alias, rid in aliases.items():
        # user ids are not document aliases: a register listing many users would link them all
        if alias and not re.fullmatch(r"user_\d+", alias) and \
                re.search(r"(?<![\w.])" + re.escape(alias) + r"(?![\w])", text):
            rids.add(rid)
    doc_keys = text_keys(text)
    lines = text.splitlines()
    for rid in sorted(rids):
        if len(rids) == 1:
            keys = doc_keys
        else:   # several resources: only identifiers printed next to this one
            keys = set()
            for i, ln in enumerate(lines):
                if rid in ln or any(a in ln for a, r in aliases.items() if r == rid):
                    keys |= text_keys(" ".join(lines[i:i + 2]))
        system = system_of(rid)
        obs.append(Observation(eid, ftime, ftime, system, rid, "PRESENT", keys, hint))
    return obs


# ---------------------------------------------------------------- activity-derived observations


def activity_observations(activities):
    """Verification-style activities that report an observed state (not just a job result)."""
    obs = []
    for a in activities:
        t = parse_ts(a["event_time"])
        et, det = a["event_type"], a["details"]
        if et == "CHECK_DATA_FLOW" and "remains enabled" in det:
            obs.append(Observation(a["activity_id"], t, t, "integration_service", a["resource_id"], "ACTIVE",
                                   set(), "check", "activity"))
        elif et == "CHECK_DATA_FLOW" and ("disabled" in det or "inactive" in det):
            obs.append(Observation(a["activity_id"], t, t, "integration_service", a["resource_id"], "DISABLED",
                                   set(), "check", "activity"))
        elif et == "CHECK_RESTORE_ACCESS" and "can still be restored" in det:
            obs.append(Observation(a["activity_id"], t, t, "backup_service", a["resource_id"], "ACTIVE",
                                   set(), "check", "activity"))
        elif et == "CHECK_RESTORE_ACCESS" and ("blocked" in det or "cannot" in det):
            obs.append(Observation(a["activity_id"], t, t, "backup_service", a["resource_id"], "BLOCKED",
                                   set(), "check", "activity"))
        elif et == "RESTORE_BACKUP" and a["reported_result"] in ("SUCCESS", "PARTIAL"):
            obs.append(Observation(a["activity_id"], t, t, "backup_service", a["resource_id"], "ACTIVE",
                                   set(), "restore", "activity"))
            for rid in RESOURCE_RE.finditer(det):
                r = rid.group(0)
                if r != a["resource_id"]:
                    obs.append(Observation(a["activity_id"], t, t, system_of(r), r, "PRESENT",
                                           {"res:" + a["resource_id"]}, "restored", "activity"))
    return obs


# ---------------------------------------------------------------- loader


class EvidenceStore:
    def __init__(self, activities, catalog_aliases):
        self.manifest = {r["file_id"]: r for r in read_csv("evidence_files.csv")}
        self.file_time = {k: parse_ts(v["captured_at"]) for k, v in self.manifest.items()}
        self.observations = []
        self.sql_coverage = {}       # file_id -> set(systems)
        self.sql_present = {}        # file_id -> set(resource_id)
        self.errors = []
        for eid, row in sorted(self.manifest.items()):
            path = DATASET / row["file_path"]
            ftime = self.file_time[eid]
            try:
                obs = self._parse(path, eid, ftime, catalog_aliases)
            except Exception as exc:  # keep going; one bad file must not stop the audit
                self.errors.append(f"{eid}: {exc}")
                obs = []
            self.observations.extend(obs)
        self.observations.extend(activity_observations(activities))
        self.by_resource = defaultdict(list)
        for o in self.observations:
            self.by_resource[o.resource_id].append(o)

    def _parse(self, path, eid, ftime, aliases):
        suf = path.suffix.lower()
        if suf == ".json":
            return parse_json(path, eid, ftime)
        if suf == ".txt":
            return parse_text(path, eid, ftime)
        if suf == ".log":
            return parse_log(path, eid, ftime)
        if suf == ".sql":
            obs, covered = parse_sql(path, eid, ftime)
            self.sql_coverage[eid] = covered
            self.sql_present[eid] = {o.resource_id for o in obs}
            return obs
        if suf == ".pdf":
            return parse_document_text(pdf_text(path), eid, ftime, aliases, "document:" + path.stem)
        if suf in (".png", ".jpg", ".jpeg"):
            return vision.image_observations(path, eid, ftime, aliases, Observation, derive_state)
        return []
