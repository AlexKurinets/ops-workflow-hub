#!/usr/bin/env bash
# Bring the Compose stack up, push synthetic events through the real API, and check that the
# worker and scheduler finished them. Used by CI and handy after any change to the stack.
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d --wait
HOST="localhost:${API_PORT:-8000}"

echo "health: $(curl -fsS $HOST/health)"

docker compose exec -T api hub simulate placement --api-url http://localhost:8000 > /dev/null
docker compose exec -T api hub simulate duplicate --api-url http://localhost:8000 > /dev/null
docker compose exec -T api hub simulate lead --api-url http://localhost:8000 > /dev/null
docker compose exec -T api hub simulate timesheet --mismatch > /dev/null
docker compose exec -T api hub scheduler --once > /dev/null
docker compose exec -T api hub trigger nightly_payroll_reconciliation > /dev/null

for _ in $(seq 1 45); do
  succeeded=$(curl -fsS "$HOST/runs?status=succeeded" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')
  pending=$(curl -fsS "$HOST/stats" | python3 -c 'import json,sys; s=json.load(sys.stdin)["by_status"]; print(s["pending"]+s["running"]+s["waiting_retry"])')
  if [ "$pending" = "0" ] && [ "$succeeded" -ge 8 ]; then
    echo "smoke ok: $succeeded runs succeeded"
    curl -fsS $HOST/stats
    echo
    exit 0
  fi
  sleep 2
done

echo "smoke failed: runs did not finish"
curl -fsS "$HOST/runs" || true
docker compose logs worker scheduler
exit 1
