"""route_record.py — record a claimed task's resolved route on its ledger
entry (Chunk 4: portal route visibility).

Called by run.sh immediately after a won exclusive claim. Contract: NEVER
fail the cycle — every problem is a stderr warning and exit 0 (run.sh also
appends `|| true`, belt and braces). Reads the task's category from
$AP_STATE/queue.json, resolves it via route_resolve (override-else-default,
the same rule the orchestrator uses), and writes through ledger.cmd_route
so the attach happens under the ledger lock.
"""
import json
import os
import sys

import ledger
import route_resolve


def main(argv):
    if not argv:
        print("route_record: no task id given — skipping", file=sys.stderr)
        return 0
    tid = str(argv[0])
    ap_state = os.environ.get("AP_STATE") or os.path.join(
        os.environ.get("AP_HOME", ""), "state")
    try:
        with open(os.path.join(ap_state, "queue.json")) as f:
            tasks = json.load(f).get("tasks", [])
    except (OSError, ValueError) as e:
        print(f"route_record: cannot read queue.json ({e}) — skipping",
              file=sys.stderr)
        return 0
    cat = next((t.get("category") for t in tasks
                if str(t.get("id")) == tid), None)
    if not cat:
        print(f"route_record: task {tid} not in queue.json — skipping",
              file=sys.stderr)
        return 0
    router = route_resolve.load_router(os.environ.get("AP_HOME"))
    route = route_resolve.resolve_route(cat, router)
    if not route:
        print(f"route_record: no route for category '{cat}' — skipping",
              file=sys.stderr)
        return 0
    record = dict(route)
    record.update(source=router["source"], router_path=router["path"])
    try:
        ledger.cmd_route(tid, json.dumps(record))
    except SystemExit as e:
        print(f"route_record: ledger refused (exit {e.code}) — skipping",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
