
"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python code/main.py                       # audits dataset/deletion_requests.csv -> output.csv
    python code/main.py --input sample_deletion_requests.csv --output code/evaluation/sample_predictions.csv
    python code/main.py --llm                 # model labels every request text (cross-check + undecidable texts)
    python code/main.py --refresh-vision      # re-extract screenshot facts with the model (rewrites the cache)

The model is Gemini when GEMINI_API_KEY is set, otherwise Claude (see code/ghost_audit/llm.py).
"""
import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.common import DATASET, REPO_ROOT, read_csv  # noqa: E402
from ghost_audit import intent, llm, vision  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default="deletion_requests.csv", help="request file inside dataset/")
    ap.add_argument("--output", default=str(REPO_ROOT / "output.csv"))
    ap.add_argument("--llm", action="store_true", help="label every request text with the model")
    ap.add_argument("--refresh-vision", action="store_true", help="re-extract screenshots with the model")
    ap.add_argument("--debug", help="write per-case reasoning JSON to this path")
    ap.add_argument("--explain", metavar="CASE_ID", help="print the agent's reasoning for one case and exit")
    args = ap.parse_args(argv)
    if args.explain:
        return explain(args.explain)

    started = time.time()
    if (args.refresh_vision or args.llm) and not llm.available():
        sys.exit("no model configured: set GEMINI_API_KEY (Gemini) or install `anthropic` and set ANTHROPIC_API_KEY")
    if args.refresh_vision:
        vision.refresh(sorted((DATASET / "evidence" / "images").glob("*.png")))

    from ghost_audit.audit import World, CaseAudit
    # every participant-facing request helps decide which workflow run belongs to whom
    all_requests = read_csv("sample_deletion_requests.csv") + read_csv("deletion_requests.csv")
    intent_check = None
    if args.llm:   # label before account assignment, which depends on intent
        intent_check = intent.classify_all([q["request_text"] for q in read_csv(args.input)])
        print(f"intent cross-check: model agrees with rules on {intent_check['agree']}/{intent_check['labelled']} texts")
    world = World(use_llm=args.llm)
    world.assign_accounts(all_requests)

    def case_order(r):   # case_33 < case_100 (numeric, matching the input file)
        tail = r["case_id"].rsplit("_", 1)[-1]
        return (int(tail) if tail.isdigit() else float("inf"), r["case_id"])

    requests = sorted(read_csv(args.input), key=case_order)
    rows, debug = [], {}
    for q in requests:
        status, steps, info = CaseAudit(world, q).run()
        rows.append((q["case_id"], status, json.dumps(steps, ensure_ascii=False, separators=(",", ":"))))
        debug[q["case_id"]] = {"status": status, **info}

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f, lineterminator="\n")
        wr.writerow(["case_id", "deletion_status", "steps_json"])
        wr.writerows(rows)
    if args.debug:
        Path(args.debug).write_text(json.dumps(debug, indent=1, default=str), encoding="utf-8")

    counts = {}
    for _, s, _ in rows:
        counts[s] = counts.get(s, 0) + 1
    if args.input == "deletion_requests.csv":   # the usage report describes the run that produced output.csv
        flags = " ".join(f for f, on in (("--llm", args.llm), ("--refresh-vision", args.refresh_vision)) if on)
        usage = llm.save_usage({"requests": len(rows), "seconds": round(time.time() - started, 1), "input": args.input,
                                "command": (f"GHOST_AUDIT_PROVIDER={llm.provider()} " if os.environ.get("GHOST_AUDIT_PROVIDER")
                                            else "") + f"python code/main.py {flags}".strip(),
                                "intent_check": intent_check})
        llm.write_usage_report(usage)
    if world.store.errors:
        print("evidence parse errors:", *world.store.errors, sep="\n  ")
    print(f"wrote {len(rows)} rows to {out}  {counts}  model calls={llm.USAGE['calls']}")


def explain(case_id):
    """Walk through one case: request, identity, records found, state at audit_at, final steps."""
    from ghost_audit.audit import World, CaseAudit
    from ghost_audit import intent
    world = World()
    reqs = read_csv("sample_deletion_requests.csv") + read_csv("deletion_requests.csv")
    world.assign_accounts(reqs)
    q = next((r for r in reqs if r["case_id"] == case_id), None)
    if not q:
        sys.exit(f"unknown case {case_id}")
    audit = CaseAudit(world, q)
    print(f"\n1. REQUEST   {case_id}  email={q['email']}  workflow says={q['reported_status']}  audit_at={q['audit_at']}")
    print(f"   text: {q['request_text'][:160]}")
    print(f"   asks for deletion? {intent.authorises_deletion(q['request_text'])}")
    uid = audit.uid
    print(f"\n2. IDENTITY  user={uid}")
    for k in sorted(world.user_keys.get(uid, [])):
        if not k.startswith("hash:"):
            print(f"   - {k}")
    print("\n3. RECORDS FOUND -> latest state visible at audit_at (evidence)")
    if uid and intent.authorises_deletion(q["request_text"]):
        for rid, how in sorted(audit.discover().items()):
            o = audit.state_of(rid)
            state = f"{o.state:12s} ({o.evidence_id})" if o else "not visible before audit_at"
            print(f"   {rid:20s} linked via {how:22s} state: {state}")
    status, steps, _ = audit.run()
    print(f"\n4. VERDICT   {status}")
    for s in steps:
        print(f"   {s['step_no']:>2}. {s['phase']:12s} {s['resource_id']:20s} {s['operation']:28s} {s['evidence_id']}")
    print()


if __name__ == "__main__":
    main()
