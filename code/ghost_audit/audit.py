"""Case assembly: identity resolution -> resource discovery -> state at audit_at -> plan."""
import hashlib
import re
from collections import defaultdict

from .common import parse_ts, prefix_of, read_csv, system_of
from .evidence import EvidenceStore, Observation
from . import intent
from .planner import build_steps

LINK_RE = re.compile(r"linked (\S+) \(([^)]*)\) to (\S+) and (user_\d+)")
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
ATTACH_RE = re.compile(r"confirmed (\S+) \(([^)]*)\) belongs only to (\S+) and (user_\d+)")

# A deletion job runs one subject's policy actions after its account deletion at a fixed cadence,
# so (action time - account deletion time) mod 60 s is the same for every job of one action kind.
JOB_WINDOW = 6 * 3600       # policy actions follow the account deletion within a few hours
JOB_MIN_SUPPORT = 3         # identifier-linked examples needed before a cadence is trusted
JOB_MIN_SHARE = 0.2         # a residue must hold this share of the examples to count
JOB_PEAK_RATIO = 4.0        # unsupervised: peak residue vs the mean of all 60 residues
JOB_PEAK_MARGIN = 1.5       # unsupervised: peak vs the best residue outside the peak
ID_TOKEN_RE = re.compile(r"\b[a-z_]+_\d+\b")


def action_kind(activity):
    """(resource kind, message template): completed policy actions and retries run on different cadences."""
    return prefix_of(activity["resource_id"]), ID_TOKEN_RE.sub("X", activity["details"])


class World:
    """Everything that is shared by all cases (loaded once)."""

    def __init__(self, use_llm=False):
        self.use_llm = use_llm
        self.identities = {r["user_id"]: r for r in read_csv("user_identities.csv")}
        self.audit_log = read_csv("user_audit_log.csv")
        self.activities = read_csv("system_activity.csv")
        self.rules = read_csv("deletion_rules.csv")
        self._build_identity_index()
        self._build_catalog()
        self.store = EvidenceStore(self.activities, self.aliases)
        self.activity_by_resource = defaultdict(list)
        for a in self.activities:
            self.activity_by_resource[a["resource_id"]].append(a)
        self._index_owners()

    # ------------------------------------------------------------ identities
    def _build_identity_index(self):
        self.user_keys = defaultdict(set)
        self.email_owner = {}
        self.key_owner = defaultdict(set)
        for uid, r in self.identities.items():
            self._add_key(uid, "uid:" + uid.lower())
            self._add_email(uid, r["email"])
            if r["email_hash"]:
                self._add_key(uid, "hash:" + r["email_hash"].lower())
            self._add_phone(uid, r["phone"])
        for a in self.audit_log:
            uid = a["user_id"]
            for v in (a["old_value"], a["new_value"]):
                if not v:
                    continue
                if a["field_name"] == "email":
                    self._add_email(uid, v)
                elif a["field_name"] == "email_hash":
                    self._add_key(uid, "hash:" + v.lower())
                elif a["field_name"] == "phone":
                    self._add_phone(uid, v)

    def _add_key(self, uid, key):
        self.user_keys[uid].add(key)
        self.key_owner[key].add(uid)

    def _add_email(self, uid, email):
        e = email.strip().lower()
        self.email_owner.setdefault(e, uid)
        self._add_key(uid, "email:" + e)
        self._add_key(uid, "hash:" + hashlib.sha256(e.encode()).hexdigest())
        self._add_key(uid, "hash:" + hashlib.sha256(email.strip().encode()).hexdigest())

    def _add_phone(self, uid, phone):
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) >= 8:
            self._add_key(uid, "tel:" + digits)

    def resolve_user(self, email):
        return self.email_owner.get((email or "").strip().lower())

    # ------------------------------------------------------------ data catalog
    def _build_catalog(self):
        self.aliases = {}
        self.catalog = []   # (time, resource_id, user_id, activity_id)
        for a in self.activities:
            if a["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = LINK_RE.search(a["details"]) or ATTACH_RE.search(a["details"])
            if not m:
                continue
            rid, alias, email, uid = m.groups()
            if alias and alias != rid:
                self.aliases[alias] = rid
            self.catalog.append((parse_ts(a["event_time"]), rid, uid, a["activity_id"]))

    # ------------------------------------------------------------ owners
    def _index_owners(self):
        """resource -> users that any observation (any time) ties it to."""
        self.owners = defaultdict(set)
        for o in self.store.observations:
            for k in o.keys:
                for uid in self.key_owner.get(k, ()):
                    self.owners[o.resource_id].add(uid)
        for _, rid, uid, _ in self.catalog:
            self.owners[rid].add(uid)

    # ------------------------------------------------------------ deletion jobs
    def _index_jobs(self):
        """Deletion-worker activity: account deletion times, and the policy actions that follow them."""
        self.account_deleted_at = defaultdict(list)   # account -> [time]
        self.job_actions = defaultdict(list)          # resource -> [(time, activity_id, kind)]
        for a in self.activities:
            if a["source_system"] != "deletion_worker":
                continue
            t = parse_ts(a["event_time"])
            if a["target_system"] == "account_service" and a["event_type"] == "DELETE_RECORD":
                self.account_deleted_at[a["resource_id"]].append(t)
            elif prefix_of(a["resource_id"]) not in (None, "account"):
                self.job_actions[a["resource_id"]].append((t, a["activity_id"], action_kind(a)))
        self._learn_cadence()

    def _residues(self, accounts, actions):
        for ta in accounts:
            for t, aid, kind in actions:
                d = (t - ta).total_seconds()
                if 0 < d < JOB_WINDOW:
                    yield kind, int(d) % 60, aid

    def _learn_cadence(self):
        """Learn, per action kind, the residues (seconds mod 60) that pair a policy action with its
        account deletion.

        Supervised first: identifier-linked subject -> account -> action pairs (never the activity-time
        guesses). Kinds with no such pairs (an anonymized analytics profile carries no identifier) are
        learned without labels: every action belongs to exactly one job, so pairing it with every
        account deletion in the window piles the true residue into a sharp peak over uniform noise.
        """
        labelled = defaultdict(lambda: defaultdict(int))
        for uid in {u for us in self.owners.values() for u in us}:
            accts = [r for r, us in self.owners.items() if prefix_of(r) == "account" and uid in us
                     and r in self.account_deleted_at]
            if len(accts) != 1:
                continue
            for rid, us in self.owners.items():
                if us == {uid} and rid in self.job_actions:
                    for kind, res, _ in self._residues(self.account_deleted_at[accts[0]], self.job_actions[rid]):
                        labelled[kind][res] += 1
        unlabelled = defaultdict(lambda: defaultdict(int))
        all_deletions = [t for ts in self.account_deleted_at.values() for t in ts]
        for actions in self.job_actions.values():
            for kind, res, _ in self._residues(all_deletions, actions):
                unlabelled[kind][res] += 1
        self.cadence, self.cadence_source = {}, {}
        for kind in set(labelled) | set(unlabelled):
            c = labelled.get(kind, {})
            n = sum(c.values())
            if n >= JOB_MIN_SUPPORT:
                self.cadence[kind] = {r for r, k in c.items() if k / n >= JOB_MIN_SHARE}
                self.cadence_source[kind] = "identifier-linked"
                continue
            c = unlabelled.get(kind, {})
            if not c:
                continue
            peak = max(sorted(c), key=lambda r: c[r])
            near = {peak, (peak - 1) % 60, (peak + 1) % 60}
            outside = max((k for r, k in c.items() if r not in near), default=0)
            if c[peak] >= JOB_MIN_SUPPORT and c[peak] >= JOB_PEAK_RATIO * sum(c.values()) / 60 \
                    and c[peak] >= JOB_PEAK_MARGIN * outside:
                self.cadence[kind] = {r for r in near if c.get(r, 0) >= 0.4 * c[peak]}
                self.cadence_source[kind] = "unsupervised-peak"

    def job_aligned(self, account, rid):
        """Activity id of a policy action on `rid` that sits on `account`'s deletion-job cadence."""
        for kind, res, aid in self._residues(self.account_deleted_at.get(account, ()), self.job_actions.get(rid, ())):
            if res in self.cadence.get(kind, ()):
                return aid
        return None

    def has_cadence(self, rid):
        return any(kind in self.cadence for _, _, kind in self.job_actions.get(rid, ()))

    def _link_jobs(self):
        """Give each policy action that no identifier ties to anyone (e.g. an analytics profile that
        was already anonymized) to the one case whose account-deletion job it belongs to."""
        self.job_links = defaultdict(dict)   # case_id -> {resource: activity_id}
        for rid in self.job_actions:
            if self.owners.get(rid) or not self.has_cadence(rid):
                continue
            hits = {}
            for cid, accts in self.account_of.items():
                for acct in accts:
                    aid = self.job_aligned(acct, rid)
                    if aid:
                        hits[cid] = aid
            if len(hits) == 1:
                cid, aid = hits.popitem()
                self.job_links[cid][rid] = aid

    def assign_accounts(self, requests):
        """Link each deletion subject to their account.

        Identifier evidence wins. Users with no identifier-linked account get an unclaimed account
        deletion run by the deletion workflow between requested_at and audit_at: first one whose job
        cadence matches the subject's identifier-linked policy actions, then the one closest to the
        workflow's reported_at time (the workflow's own claim names the account it acted on).
        """
        if not hasattr(self, "cadence"):
            self._index_jobs()
        self.account_of = {}
        self.assigned_by_activity = set()
        self.user_cases = defaultdict(list)
        claimed = set()
        pending = []
        for q in requests:
            uid = self.resolve_user(q["email"])
            if not uid or not intent.authorises_deletion(q["request_text"]):
                continue
            self.user_cases[uid].append((q["case_id"], parse_ts(q["audit_at"])))
            accts = sorted(r for r, us in self.owners.items() if prefix_of(r) == "account" and uid in us)
            if accts:
                self.account_of[q["case_id"]] = accts
                claimed.update(accts)
            else:
                pending.append(q)
        cands = []
        for q in pending:
            req, rep, aud = parse_ts(q["requested_at"]), parse_ts(q["reported_at"]), parse_ts(q["audit_at"])
            uid = self.resolve_user(q["email"])
            own_actions = [r for r, us in self.owners.items() if us == {uid} and r in self.job_actions
                           and self.has_cadence(r)]
            for a in self.activities:
                if a["target_system"] != "account_service" or a["event_type"] != "DELETE_RECORD":
                    continue
                t = parse_ts(a["event_time"])
                if not (req <= t <= aud):
                    continue
                if self.owners.get(a["resource_id"]):
                    continue
                # the workflow's reported status is derived from its own job result
                consistent = STATUS_RESULT.get(q["reported_status"]) == a["reported_result"]
                cost = abs((t - rep).total_seconds()) + (0 if consistent else 1800)
                # 0: the subject's own policy actions run on this account's job cadence;
                # 1: nothing to compare; 2: they do not, so this is someone else's job
                if not own_actions:
                    job = 1
                elif any(self.job_aligned(a["resource_id"], r) for r in own_actions):
                    job = 0
                else:
                    job = 2
                cands.append((job, cost, a["event_time"], q["case_id"], a["resource_id"]))
        cands.sort()
        done = set()
        for _, _, _, cid, rid in cands:
            if cid in done or rid in claimed:
                continue
            self.account_of[cid] = [rid]
            self.assigned_by_activity.add((cid, rid))
            claimed.add(rid)
            done.add(cid)
        self._link_jobs()


class CaseAudit:
    def __init__(self, world, q):
        self.w = world
        self.q = q
        self.audit_at = parse_ts(q["audit_at"])
        self.requested_at = parse_ts(q["requested_at"])
        self.uid = world.resolve_user(q["email"])
        self.notes = []

    def visible(self, o):
        return o.file_time is not None and o.file_time <= self.audit_at

    def discover(self):
        w, uid = self.w, self.uid
        keys = w.user_keys.get(uid, set())
        # Identity links may come from any capture: which records belong to the subject does not
        # change over time. Their *state* is only ever read from evidence visible at audit_at.
        linked = {}
        for o in w.store.observations:
            if o.keys & keys:
                linked.setdefault(o.resource_id, o.evidence_id)
        for t, rid, cuid, aid in w.catalog:
            if cuid == uid:
                linked.setdefault(rid, aid)
        for rid in w.account_of.get(self.q["case_id"], []):
            linked.setdefault(rid, "activity-assignment")
        for rid, aid in w.job_links.get(self.q["case_id"], {}).items():
            linked.setdefault(rid, aid)
        # follow resource references (search doc -> source account, restore -> restored copy)
        changed = True
        while changed:
            changed = False
            for o in w.store.observations:
                if o.resource_id in linked:
                    continue
                refs = {k[4:] for k in o.keys if k.startswith("res:")}
                if refs & set(linked):
                    linked[o.resource_id] = o.evidence_id
                    changed = True
        out = {}
        for rid, ev in linked.items():
            if not system_of(rid):
                continue
            if not self.owns_in_time(rid):
                continue
            out[rid] = ev
        return out

    def owns_in_time(self, rid):
        """When one person filed several requests, give each record to the request it was seen for:
        the earliest request whose audit_at is at or after the record's first sighting."""
        cases = self.w.user_cases.get(self.uid, [])
        if len(cases) < 2:
            return True
        if (self.q["case_id"], rid) in self.w.assigned_by_activity or \
                rid in self.w.job_links.get(self.q["case_id"], {}):
            return True
        times = [o.ts or o.file_time for o in self.w.store.by_resource.get(rid, []) if o.keys & self.w.user_keys[self.uid]]
        times += [t for t, r, u, _ in self.w.catalog if r == rid and u == self.uid]
        if not times:
            return True
        first = min(times)
        owner = None
        for cid, aud in sorted(cases, key=lambda x: x[1]):
            if aud >= first:
                owner = cid
                break
        if owner is None:
            owner = max(cases, key=lambda x: x[1])[0]
        return owner == self.q["case_id"]

    def state_of(self, rid):
        """Latest visible observation for a resource (SQL snapshots also prove absence)."""
        obs = [o for o in self.w.store.by_resource.get(rid, []) if self.visible(o) and o.state]
        system = system_of(rid)
        present_in = set()
        for o in obs:
            present_in.add(o.evidence_id)
        retained = prefix_of(rid) in ("audit_record", "legal_hold")   # absence proves nothing useful here
        for eid, covered in ({} if retained else self.w.store.sql_coverage).items():
            ft = self.w.store.file_time[eid]
            # a snapshot taken before the request says nothing about whether the deletion happened
            if self.requested_at <= ft <= self.audit_at and system in covered \
                    and rid not in self.w.store.sql_present[eid]:
                obs.append(Observation(eid, ft, ft, system, rid, "ABSENT", set(), "sql_absence", "sql_absence"))
        if not obs:
            return None
        obs.sort(key=lambda o: (o.ts or o.file_time, o.file_time, o.source != "sql_absence", o.evidence_id))
        return obs[-1]

    def run(self):
        q = self.q
        if not intent.authorises_deletion(q["request_text"], use_llm=self.w.use_llm):
            return "NOT_APPLICABLE", [], {"reason": "request does not authorise deletion"}
        if not self.uid:
            return "NOT_APPLICABLE", [], {"reason": "requester identity not found"}
        linked = self.discover()
        # a visible deleted-account tombstone for this subject is evidence that the profile is gone
        uid_key = "uid:" + self.uid
        tomb = [o for o in self.w.store.observations if o.system == "identity_service" and self.visible(o)
                and uid_key in o.keys]
        tomb.sort(key=lambda o: (o.ts or o.file_time, o.evidence_id))
        resources = {}
        for rid in linked:
            latest = self.state_of(rid)
            if latest is None and prefix_of(rid) == "account" and tomb:
                t = tomb[-1]
                latest = Observation(t.evidence_id, t.file_time, t.ts, "account_service", rid, "ABSENT",
                                     set(), "tombstone", "tombstone")
            hints = {o.hint for o in self.w.store.by_resource.get(rid, [])}
            resources[rid] = {"latest": latest, "hints": hints}
        status, steps = build_steps(resources, self.w, self)
        return status, steps, {"user": self.uid, "resources": {r: (v["latest"].state if v["latest"] else None)
                                                               for r, v in resources.items()}}
