#!/usr/bin/env bash
# Bring the Compose stack up, push synthetic events through the real API, and check that the
# worker and scheduler finished them, including the dead-letter fix an operator would do.
# Used by CI and handy after any change to the stack. Set PYTHON if python3 is not on PATH.
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d --wait
HOST="localhost:${API_PORT:-8000}"
PY="${PYTHON:-python3}"

hub() { docker compose exec -T api hub "$@"; }
count() { curl -fsS "$HOST/runs?status=$1" | "$PY" -c 'import json,sys; print(len(json.load(sys.stdin)))'; }
inflight() {
  curl -fsS "$HOST/stats" \
    | "$PY" -c 'import json,sys; s=json.load(sys.stdin)["by_status"]; print(s["pending"]+s["running"]+s["waiting_retry"])'
}
fail() {
  echo "smoke failed: $1"
  curl -fsS "$HOST/runs" || true
  docker compose logs worker scheduler
  exit 1
}
settle() {  # wait until nothing is pending, running or waiting for a retry
  for _ in $(seq 1 45); do
    [ "$(inflight)" = "0" ] && return 0
    sleep 2
  done
  fail "runs did not finish"
}

echo "health: $(curl -fsS $HOST/health)"

hub simulate placement --api-url http://localhost:8000 > /dev/null
hub simulate duplicate --api-url http://localhost:8000 > /dev/null
hub simulate lead --api-url http://localhost:8000 > /dev/null
hub simulate timesheet --mismatch > /dev/null
hub scheduler --once > /dev/null
hub trigger nightly_payroll_reconciliation > /dev/null
settle
[ "$(count succeeded)" -ge 8 ] || fail "expected at least 8 succeeded runs, got $(count succeeded)"
[ "$(count dead_lettered)" = "0" ] || fail "unexpected dead-lettered run"

# A timesheet for a candidate with no payroll worker parks at require_worker; the operator
# creates the worker, links it and replays from the lookup step.
hub simulate timesheet --unlinked > /dev/null
hub scheduler --once > /dev/null
settle
[ "$(count dead_lettered)" = "1" ] || fail "expected exactly one dead-lettered run"
dead=$(curl -fsS "$HOST/runs?status=dead_lettered" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
hub simulate payroll-worker --candidate cand_1006 > /dev/null
hub link person ats:cand_1006 payroll:wk_7006 > /dev/null
hub replay "$dead" --from-step find_worker > /dev/null
settle
[ "$(count dead_lettered)" = "0" ] || fail "replay did not clear the dead-lettered run"
[ "$(count succeeded)" -ge 9 ] || fail "expected at least 9 succeeded runs after the replay"
hours=$(curl -fsS "$HOST/sandbox/payroll/hours" \
  | "$PY" -c 'import json,sys; print(sum(1 for r in json.load(sys.stdin) if r["worker_id"]=="wk_7006"))')
[ "$hours" = "1" ] || fail "expected one hours record for wk_7006, got $hours"

echo "smoke ok: $(count succeeded) runs succeeded, dead-letter fixed by replay"
curl -fsS $HOST/stats
echo
