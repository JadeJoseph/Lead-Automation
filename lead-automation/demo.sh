#!/usr/bin/env bash
# Screen-recording helper. Press Enter between steps so YOU control the pace while narrating.
# NOPAUSE=1 ./demo.sh   runs straight through (for a dry run).
set -e
cd "$(dirname "$0")"

pause() { [ -n "$NOPAUSE" ] || read -r -p $'\n[Enter for next step] '; echo; }
step()  { printf '\n\033[1m== %s ==\033[0m\n\n' "$1"; }

step "1. Run the workflow on the 15 sample leads (clean > dedupe > triage > sheet > notify)"
python3 -m leadflow run --reset
pause

step "2. The leads sheet: what was added, cleaned and flagged"
python3 - <<'EOF'
import csv
rows = list(csv.DictReader(open("out/leads.csv", encoding="utf-8")))
print(f"{len(rows)} rows (12 existing + {len(rows) - 12} new). Original 7 columns kept; new ones appended.\n")
print(f"{'ID':<8}{'NAME':<18}{'EMAIL / PHONE':<30}{'TAG':<6}FLAGS")
for r in rows[12:]:
    print(f"{r['lead_id']:<8}{r['name']:<18}{(r['email'] or r['phone']):<30}{r['tag']:<6}{r['flags']}")
l8 = next(r for r in rows if r["lead_id"] == "L-1008")
print(f"\nExisting row L-1008 (Tom Walsh) is now {l8['tag']}: flags = {l8['flags']}")
EOF
pause

step "3. One draft reply: the German Hot lead (S-09), and the injection lead (S-10)"
python3 - <<'EOF'
import csv
rows = {r["source_id"]: r for r in csv.DictReader(open("out/leads.csv", encoding="utf-8"))}
for sid in ("S-09", "S-10"):
    r = rows[sid]
    print(f"--- {sid} {r['name']} [{r['tag']}] mode={r['triage_mode']}\n{r['draft_reply']}\n")
EOF
pause

step "4. Hot lead notifications"
python3 -c "
import json
for l in open('out/notifications.jsonl'): print(json.loads(l)['text'], '\n')"
pause

step "5. Review queue: everything that was NOT a clean add, with the reason"
python3 - <<'EOF'
import csv
for r in csv.DictReader(open("out/review_queue.csv", encoding="utf-8")):
    print(f"{r['source_id']:<6}{r['outcome']:<11}{r['stage']:<9}{r['reason'][:95]}")
EOF
pause

step "6. Re-run the same file: nothing is duplicated"
python3 -m leadflow run | tail -8
pause

step "7. Tests"
python3 -m unittest discover -s tests 2>&1 | tail -4
