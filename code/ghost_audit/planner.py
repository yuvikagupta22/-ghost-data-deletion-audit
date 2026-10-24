"""Policy engine and step builder.

Maps every discovered resource to a deletion_rules.csv category, compares its state at audit_at
with the state the rule requires, and emits FINDING -> REMEDIATION -> VERIFICATION steps.
"""
from .common import prefix_of, system_of

FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# treatment -> (finding op, remediation op, verification op)
TREATMENT = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}
COMPLIANT = {"DELETE": {"ABSENT"}, "REDACT": {"REDACTED"}, "ANONYMIZE": {"ANONYMIZED"},
             "RETAIN": {"PRESENT", "IDENTIFIABLE", "REDACTED", "ANONYMIZED"}, "DISABLE": {"DISABLED"},
             "BLOCK": {"BLOCKED"}}


def category(rid, hints):
    """deletion_rules.csv data_category for a resource."""
    p = prefix_of(rid)
    h = " ".join(hints).lower()
    if p == "account":
        return "USER_PROFILE"
    if p == "search":
        return "SEARCH_INDEX_ENTRY"
    if p == "cache":
        return "CACHE_ENTRY"
    if p == "support":
        if any(x in h for x in ("attachment", "contact_query", "deletion_marker", "document:")):
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_MESSAGE"
    if p == "object":
        return "SUPPORT_ATTACHMENT"
    if p == "analytics":
        if "contact_lookup" in h:
            return "SECONDARY_IDENTIFIER"
        return "ANALYTICS_PROFILE"
    if p == "telemetry":
        return "PRODUCT_TELEMETRY"
    if p == "export":
        return "USER_EXPORT"
    if p == "marketing":
        return "MARKETING_PROFILE"
    if p == "email_delivery":
        return "EMAIL_DELIVERY_LOG"
    if p == "audit_record":
        return "SECURITY_AUDIT_LOG"
    if p == "legal_hold":
        return "LEGAL_HOLD_RECORD"
    if p == "backup":
        return "BACKUP_ARCHIVE"
    if p == "tombstone":
        return "ACCOUNT_TOMBSTONE"
    if p == "data_flow":
        return "DATA_FLOW"
    return None


# RETAIN rules whose condition is a time-bounded period ("within the supplied retention period",
# "a supplied legal hold is active"): once the record's own stated end has passed, nothing requires
# keeping it any more and it is deleted like any other personal record.
TIME_BOUNDED_RETAIN = {"SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"}


def retention_lapsed(rid, case):
    """True when the evidence visible at audit_at shows the record's retention/hold already ended."""
    ends = [o.retain_until for o in case.w.store.by_resource.get(rid, [])
            if case.visible(o) and getattr(o, "retain_until", None)]
    return bool(ends) and max(ends) < case.audit_at


def treatment(cat, rules, rid=None, case=None):
    if cat == "DATA_FLOW":
        return "DISABLE"
    if cat == "BACKUP_ARCHIVE":
        # rule_14 retains the archive itself; its condition ("restore access can be blocked") is
        # what the plan must enforce, so the step is about the restore path, not the archive
        return "BLOCK"
    for r in rules:
        if r["data_category"] == cat:
            if r["required_action"] == "RETAIN" and cat in TIME_BOUNDED_RETAIN and case is not None \
                    and retention_lapsed(rid, case):
                return "DELETE"
            return r["required_action"]
    return None


def build_steps(resources, world, case):
    findings, remediation, verification = [], [], []
    unproven = []   # linked resources with no state visible at audit_at
    complete = True
    for rid in sorted(resources):
        info = resources[rid]
        latest = info["latest"]
        cat = category(rid, info["hints"])
        treat = treatment(cat, world.rules, rid, case)
        if not treat:
            continue
        if latest is None:
            v_op = TREATMENT[treat][2]
            unproven.append((v_op, system_of(rid), rid, ""))
            continue
        state = latest.state
        if treat == "BLOCK" and state == "PRESENT":
            state = "ACTIVE"   # an archive that is visible and not blocked can be restored
        f_op, r_op, v_op = TREATMENT[treat]
        system = system_of(rid)
        ok = state in COMPLIANT[treat] or (treat in ("REDACT", "ANONYMIZE") and state == "ABSENT")
        if treat == "RETAIN":
            if state == "ABSENT":
                continue
            verification.append((v_op, system, rid, latest.evidence_id))
            continue
        if ok:
            vop = "VERIFY_RECORD_ABSENT" if state == "ABSENT" else v_op
            verification.append((vop, system, rid, latest.evidence_id))
            continue
        complete = False
        findings.append((f_op, system, rid, latest.evidence_id))
        remediation.append((r_op, system, rid, ""))
        verification.append((v_op, system, rid, ""))

    def key(order):
        return lambda s: (order.index(s[0]), s[1], s[2])

    if not complete:
        # the post-remediation checks must also cover the required state of records we could not see
        verification += unproven
    findings.sort(key=key(FINDING_ORDER))
    remediation.sort(key=key(REMEDIATION_ORDER))
    verification.sort(key=key(VERIFICATION_ORDER))
    if complete:
        phases = [("VERIFICATION", verification)]
        status = "COMPLETE"
    else:
        # future checks carry no evidence
        verification = [(op, s, r, "") for op, s, r, _ in verification]
        phases = [("FINDING", findings), ("REMEDIATION", remediation), ("VERIFICATION", verification)]
        status = "INCOMPLETE"
    steps = []
    for phase, items in phases:
        for op, system, rid, ev in items:
            steps.append({"step_no": len(steps) + 1, "phase": phase, "system_id": system, "resource_id": rid,
                          "operation": op, "state": OP_STATE[op], "evidence_id": ev})
    return status, steps
