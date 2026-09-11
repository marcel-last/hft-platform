#!/bin/sh
# =============================================================================
# build-all.sh — build every HFT platform service (15 total).
#
#   Python (10): byte-compile each service's src package (stdlib only — no
#                pip installs are part of the build).
#   Rust   (5): cargo build (zero external crates, offline-safe; pinned lock).
#
# POSIX sh compatible (no brace expansion, no arrays, no pipefail). Safe to
# run from any directory; exits non-zero if any service fails to build.
# =============================================================================
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.cargo/bin:$PATH"

# dir:package pairs (POSIX-safe: single list, split on space)
PY_SERVICES="
  market-data-gateway:mdg
  order-book-builder:obb
  strategy-engine:ste
  risk-manager:rkm
  position-keeper:posk
  data-quality-monitor:dqm
  portfolio-analytics:pfa
  config-service:cfgs
  audit-logger:audl
  settlement-service:stls
"
RS_SERVICES="
  execution-gateway
  latency-monitor
  alerting-service
  auth-service
  api-gateway
"

fail=0

# --- Python: byte-compile every module of each service -----------------------
for entry in $PY_SERVICES; do
  dir="${entry%%:*}"
  pkg="${entry##*:}"
  src="$ROOT/services/$dir/src/$pkg"
  echo "== python build: $dir (pkg $pkg)"
  out="$(python3 -m compileall -q "$src" 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$out" | head -30
    echo "   FAILED: $dir"
    fail=1
  else
    echo "   OK"
  fi
done

# --- Rust: cargo build per crate ---------------------------------------------
for svc in $RS_SERVICES; do
  echo "== rust build: $svc"
  out="$(cd "$ROOT/services/$svc" && cargo build --message-format=short 2>&1)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$out" | head -40
    echo "   FAILED: $svc"
    fail=1
  else
    echo "   OK"
  fi
done

echo "-------------------------------------------------------------"
if [ "$fail" -ne 0 ]; then
  echo "build-all: FAILURES (see above)"
else
  echo "build-all: all 15 services built OK"
fi
exit "$fail"
