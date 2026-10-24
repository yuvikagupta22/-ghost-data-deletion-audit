"""Hand-built evaluation cases that the public samples do not cover.

Each case is a tiny synthetic world (identities, audit log, activity, evidence observations) run
through the real CaseAudit + planner code, and compared with the answer we expect from the rules.

    python code/evaluation/custom_cases.py
"""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ghost_audit.audit import CaseAudit, World  # noqa: E402
from ghost_audit.common import parse_ts, read_csv  # noqa: E402
from ghost_audit.evidence import Observation  # noqa: E402


class FakeStore:
    def __init__(self, observations, sql=None):
        self.observations = observations
        self.by_resource = defaultdict(list)
        for o in observations:
            self.by_resource[o.resource_id].append(o)
        self.sql_coverage = {k: v[0] for k, v in (sql or {}).items()}
        self.sql_present = {k: v[1] for k, v in (sql or {}).items()}
        self.file_time = {k: v[2] for k, v in (sql or {}).items()}
        self.errors = []


def make_world(identities, audit_log, activities, observations, requests, sql=None):
    w = World.__new__(World)
    w.use_llm = False
    w.identities = {r["user_id"]: r for r in identities}
    w.audit_log = audit_log
    w.activities = activities
    w.rules = read_csv("deletion_rules.csv")
    w._build_identity_index()
    w._build_catalog()
    w.store = FakeStore(observations, sql)
    w.activity_by_resource = defaultdict(list)
    w._index_owners()
    w.assign_accounts(requests)
    return w


def obs(eid, ts, rid, state, keys, hint=""):
    t = parse_ts(ts)
    from ghost_audit.common import system_of
    return Observation(eid, t, t, system_of(rid), rid, state, set(keys), hint)


def ident(uid, email, phone="+1 555 0100 000"):
    import hashlib
    return {"user_id": uid, "name": "Test Person", "email": email, "phone": phone,
            "email_hash": hashlib.sha256(email.encode()).hexdigest(), "updated_at": "2026-01-01T00:00:00Z"}


def req(cid, email, text, audit="2026-09-30T12:00:00Z", status="COMPLETE"):
    return {"case_id": cid, "email": email, "requested_at": "2026-09-01T12:00:00Z", "reported_status": status,
            "reported_at": "2026-09-02T12:00:00Z", "audit_at": audit, "request_text": text}


def short(steps):
    return [(s["phase"][0], s["resource_id"], s["operation"], s["evidence_id"]) for s in steps]


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def historical_email_and_lookalike_decoy():
    """Request uses the OLD email; a look-alike '.jr' address belongs to someone else."""
    ids = [ident("user_900", "new.person@example.com"), ident("user_901", "old.person.jr@example.com")]
    log = [{"audit_id": "audit_x1", "user_id": "user_900", "event_time": "2026-05-01T00:00:00Z",
            "field_name": "email", "old_value": "old.person@example.com", "new_value": "new.person@example.com"}]
    o = [obs("ev_a", "2026-09-20T10:00:00Z", "account_9001", "ABSENT", ["email:new.person@example.com"]),
         obs("ev_a", "2026-09-20T10:00:00Z", "search_9002", "PRESENT", ["email:old.person@example.com"]),
         obs("ev_a", "2026-09-20T10:00:00Z", "search_9003", "PRESENT", ["email:old.person.jr@example.com"]),
         obs("ev_a", "2026-09-20T10:00:00Z", "audit_record_9004", "PRESENT", ["uid:user_900"])]
    q = req("custom_1", "old.person@example.com", "Please delete my account, including records under my old email.")
    expected = ("INCOMPLETE", [("F", "search_9002", "REPORT_REMAINING_RECORD", "ev_a"),
                               ("R", "search_9002", "DELETE_RECORD", ""),
                               ("V", "account_9001", "VERIFY_RECORD_ABSENT", ""),
                               ("V", "search_9002", "VERIFY_RECORD_ABSENT", ""),
                               ("V", "audit_record_9004", "VERIFY_RECORD_PRESENT", "")])
    return make_world(ids, log, [], o, [q]), q, expected


@case
def evidence_after_audit_is_ignored():
    """A capture taken after audit_at shows the profile again; at audit_at it was gone."""
    ids = [ident("user_910", "cutoff@example.com")]
    o = [obs("ev_before", "2026-09-10T10:00:00Z", "account_9101", "ABSENT", ["email:cutoff@example.com"]),
         obs("ev_after", "2026-10-05T10:00:00Z", "account_9101", "PRESENT", ["email:cutoff@example.com"]),
         obs("ev_before", "2026-09-10T10:00:00Z", "audit_record_9102", "PRESENT", ["uid:user_910"])]
    q = req("custom_2", "cutoff@example.com", "Please erase my account.", audit="2026-09-30T12:00:00Z")
    expected = ("COMPLETE", [("V", "account_9101", "VERIFY_RECORD_ABSENT", "ev_before"),
                             ("V", "audit_record_9102", "VERIFY_RECORD_PRESENT", "ev_before")])
    return make_world(ids, [], [], o, [q]), q, expected


@case
def flows_disabled_before_dependent_records():
    """Active sync + identifiable analytics + shared support thread + exposed tombstone."""
    ids = [ident("user_920", "order@example.com")]
    k = ["email:order@example.com"]
    o = [obs("ev_c", "2026-09-20T10:00:00Z", "account_9201", "ABSENT", k),
         obs("ev_c", "2026-09-20T10:00:00Z", "analytics_9202", "IDENTIFIABLE", k),
         obs("ev_c", "2026-09-20T10:00:00Z", "support_9203", "IDENTIFIABLE", k, "message_pii_scan"),
         obs("ev_c", "2026-09-20T10:00:00Z", "data_flow_9204", "ACTIVE", k),
         obs("ev_c", "2026-09-20T10:00:00Z", "backup_9205", "ACTIVE", k),
         obs("ev_c", "2026-09-20T10:00:00Z", "tombstone_9206", "IDENTIFIABLE", ["uid:user_920"] + k)]
    q = req("custom_3", "order@example.com", "Close my account and stop any integration that copies my data.")
    expected = ("INCOMPLETE", [
        ("F", "analytics_9202", "REPORT_EXPOSED_PII", "ev_c"), ("F", "tombstone_9206", "REPORT_EXPOSED_PII", "ev_c"),
        ("F", "support_9203", "REPORT_EXPOSED_PII", "ev_c"), ("F", "data_flow_9204", "REPORT_ACTIVE_DATA_FLOW", "ev_c"),
        ("F", "backup_9205", "REPORT_RESTORE_RISK", "ev_c"),
        ("R", "data_flow_9204", "DISABLE_DATA_FLOW", ""), ("R", "backup_9205", "BLOCK_RESTORE", ""),
        ("R", "tombstone_9206", "REDACT_RECORD", ""), ("R", "support_9203", "REDACT_RECORD", ""),
        ("R", "analytics_9202", "ANONYMIZE_RECORD", ""),
        ("V", "account_9201", "VERIFY_RECORD_ABSENT", ""), ("V", "tombstone_9206", "VERIFY_RECORD_REDACTED", ""),
        ("V", "support_9203", "VERIFY_RECORD_REDACTED", ""), ("V", "analytics_9202", "VERIFY_RECORD_ANONYMIZED", ""),
        ("V", "data_flow_9204", "VERIFY_DATA_FLOW_DISABLED", ""), ("V", "backup_9205", "VERIFY_RESTORE_BLOCKED", "")])
    return make_world(ids, [], [], o, [q]), q, expected


@case
def non_deletion_request_in_spanish():
    """An access request must not trigger any deletion step."""
    ids = [ident("user_930", "acceso@example.com")]
    o = [obs("ev_d", "2026-09-20T10:00:00Z", "account_9301", "PRESENT", ["email:acceso@example.com"])]
    q = req("custom_4", "acceso@example.com",
            "Quisiera recibir una copia de mis datos personales. No quiero cerrar la cuenta.")
    return make_world(ids, [], [], o, [q]), q, ("NOT_APPLICABLE", [])


@case
def workflow_claim_does_not_prove_deletion():
    """Workflow says SUCCESS, but the replica snapshot after the request still holds the profile."""
    ids = [ident("user_940", "claim@example.com")]
    acts = [{"activity_id": "activity_x", "event_time": "2026-09-02T11:00:00Z", "event_type": "DELETE_RECORD",
             "source_system": "deletion_worker", "target_system": "account_service", "resource_id": "account_9401",
             "reported_result": "SUCCESS", "details": "Deletion worker reported completion."}]
    o = [obs("ev_e", "2026-09-10T10:00:00Z", "account_9401", "PRESENT", ["email:claim@example.com"])]
    q = req("custom_5", "claim@example.com", "I was told my account was deleted. Please remove it.")
    expected = ("INCOMPLETE", [("F", "account_9401", "REPORT_REMAINING_RECORD", "ev_e"),
                               ("R", "account_9401", "DELETE_RECORD", ""),
                               ("V", "account_9401", "VERIFY_RECORD_ABSENT", "")])
    return make_world(ids, [], acts, o, [q]), q, expected


def worker(aid, ts, rid, details, event="ANONYMIZE_RECORD", result="SUCCESS"):
    from ghost_audit.common import system_of
    return {"activity_id": aid, "event_time": ts, "event_type": event, "source_system": "deletion_worker",
            "target_system": system_of(rid), "resource_id": rid, "reported_result": result, "details": details}


@case
def deletion_job_timing_links_anonymized_analytics():
    """An anonymized analytics profile carries no identifier; only its deletion job ties it to the subject.
    Four jobs teach the cadence (+29m41s after the account deletion); an off-cadence decoy is ignored."""
    ids = [ident("user_950", "job@example.com")]
    acts, o = [], []
    for i, day in enumerate(("2026-09-02", "2026-09-04", "2026-09-06", "2026-09-08")):
        acct, ana = f"account_95{i}0", f"analytics_95{i}1"
        acts.append(worker(f"act_d{i}", f"{day}T11:01:53Z", acct,
                           f"Deletion worker submitted primary profile {acct} to account_service.", "DELETE_RECORD"))
        acts.append(worker(f"act_a{i}", f"{day}T11:31:34Z", ana, f"Policy action completed for {ana}."))
    # someone else's anonymization in the same window, off the cadence
    acts.append(worker("act_x", "2026-09-02T11:44:02Z", "analytics_9599", "Policy action completed for analytics_9599."))
    o = [obs("ev_f", "2026-09-20T10:00:00Z", "account_9500", "ABSENT", ["email:job@example.com"]),
         obs("ev_f", "2026-09-20T10:00:00Z", "analytics_9501", "ANONYMIZED", []),
         obs("ev_f", "2026-09-20T10:00:00Z", "analytics_9599", "ANONYMIZED", [])]
    q = req("custom_6", "job@example.com", "Please delete my account and all my data.")
    expected = ("COMPLETE", [("V", "account_9500", "VERIFY_RECORD_ABSENT", "ev_f"),
                             ("V", "analytics_9501", "VERIFY_RECORD_ANONYMIZED", "ev_f")])
    return make_world(ids, [], acts, o, [q]), q, expected


@case
def lapsed_retention_is_no_longer_retained():
    """rule_12 retains security audit records only within the retention period; this one's has ended."""
    ids = [ident("user_960", "retain@example.com")]
    o = [obs("ev_g", "2026-09-20T10:00:00Z", "account_9601", "ABSENT", ["email:retain@example.com"]),
         obs("ev_g", "2026-09-20T10:00:00Z", "audit_record_9602", "PRESENT", ["uid:user_960"])]
    o[1].retain_until = parse_ts("2026-06-30T23:59:59Z")
    q = req("custom_7", "retain@example.com", "Delete my account please.")
    expected = ("INCOMPLETE", [("F", "audit_record_9602", "REPORT_REMAINING_RECORD", "ev_g"),
                               ("R", "audit_record_9602", "DELETE_RECORD", ""),
                               ("V", "account_9601", "VERIFY_RECORD_ABSENT", ""),
                               ("V", "audit_record_9602", "VERIFY_RECORD_ABSENT", "")])
    return make_world(ids, [], [], o, [q]), q, expected


def run():
    passed = 0
    rows = []
    for fn in CASES:
        world, q, (exp_status, exp_steps) = fn()
        status, steps, _ = CaseAudit(world, q).run()
        ok = status == exp_status and short(steps) == exp_steps
        passed += ok
        rows.append((fn.__name__, exp_status, status, ok))
        if not ok:
            print(f"FAIL {fn.__name__}: expected {exp_status} {exp_steps}\n     got {status} {short(steps)}")
    for name, e, g, ok in rows:
        print(f"{'PASS' if ok else 'FAIL'}  {name:45s} expected={e:15s} got={g}")
    print(f"{passed}/{len(CASES)} custom cases passed")
    return passed == len(CASES)


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
