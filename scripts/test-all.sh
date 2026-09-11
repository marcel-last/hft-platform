#!/bin/sh
# =============================================================================
# test-all.sh — run every HFT platform test suite (15 total).
#
#   Python (10): python -m pytest tests/ -q   (pytest must be importable; the
#                services themselves are stdlib-only)
#   Rust   (5): cargo test -q                 (service tests + template KATs)
#
# POSIX sh compatible. Output per suite is bounded (head/tail). Exits non-zero
# if any suite fails; prints a per-service PASS/FAIL table at the end.
# =============================================================================
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.cargo/bin:$PATH"

PY_SERVICES="
  market-data-gateway
  order-book-builder
  strategy-engine
  risk-manager
  position-keeper
  data-quality-monitor
  portfolio-analytics
  config-service
  audit-logger
  settlement-service
"
RS_SERVICES="
  execution-gateway
  latency-monitor
  alerting-service
  auth-service
  api-gateway
"

fail=0
results=""

report() {  # $1=name $2=status
  results="$results$1: $2\n"
}

# --- Python suites -----------------------------------------------------------
for svc in $PY_SERVICES; do
  dir="$ROOT/services/$svc"
  echo "== python test: $svc"
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    PY=python
  fi
  out="$(cd "$dir" && PYTHONPATH=src $PY -m pytest tests/ -q 2>&1)"
  rc=$?
  tail -4 "$out" >/dev/null 2>&1  # ensure variable fully populated
  if [ "$rc" -ne 0 ]; then
    echo "$out" | tail -25
    echo "   FAILED: $svc"
    report "$svc" "FAIL"
    fail=1
  else
    echo "$out" | tail -2
    report "$svc" "PASS"
  fi
done

# --- Rust suites -------------------------------------------------------------
for svc in $RS_SERVICES; do
  echo "== rust test: $svc"
  out="$(cd "$ROOT/services/$svc" && cargo test -q 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$out" | tail -30
    echo "   FAILED: $svc"
    report "$svc" "FAIL"
    fail=1
  else
    echo "$out" | tail -3
    report "$svc" "PASS"
  fi
done

# --- Summary ------------------------------------------------------------------
echo "-------------------------------------------------------------"
printf '%b' "$results"
if [ "$fail" -ne 0 ]; then
  echo "test-all: FAILURES (see above)"
else
  echo "test-all: all 15 suites PASS"
fi
exit "$fail"
