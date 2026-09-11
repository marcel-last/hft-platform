#!/bin/sh
# =============================================================================
# smoke.sh — boot ALL 15 HFT platform services on their real ports (7610-7750),
# wait for readiness, verify GET /healthz on every port, then kill them all and
# confirm every port is freed again.
#
# POSIX sh compatible (no brace expansion / arrays). Port probing uses
# python's socket.create_connection (no `ss`/`lsof` in this environment).
# S1 runs with --mock (deterministic offline venue feeds). S14's S6 ingest
# endpoint defaults to 127.0.0.1:7660, which is exactly where S6 boots here.
#
# Table row format (one service per line, read via `while read` so the
# space-separated extra args in the 5th field survive):
#   port|kind|dir|pkg|extra-args        kind: py (python -m pkg.main) | rs
# =============================================================================
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.cargo/bin:$PATH"
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi

WORK="$(mktemp -d)"
PIDS="$WORK/pids"
TABLEF="$WORK/table"
: > "$PIDS"
: > "$TABLEF"

cat > "$TABLEF" <<'TABLE'
7610|py|market-data-gateway|mdg|--mock
7620|py|order-book-builder|obb|
7630|py|strategy-engine|ste|
7640|rs|execution-gateway|-|
7650|py|risk-manager|rkm|
7660|py|position-keeper|posk|
7670|rs|latency-monitor|-|
7680|py|data-quality-monitor|dqm|
7690|py|portfolio-analytics|pfa|
7700|rs|alerting-service|-|
7710|py|config-service|cfgs|
7720|rs|auth-service|-|--bind 127.0.0.1 --port 7720
7730|py|audit-logger|audl|
7740|py|settlement-service|stls|
7750|rs|api-gateway|-|--bind 127.0.0.1 --port 7750
TABLE

CLEANED=0
port_open() {  # $1=port -> 0 if accepting connections
  "$PY" -c "import socket,sys
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[1])), 1).close()
except OSError:
    sys.exit(1)" "$1" >/dev/null 2>&1
}

healthz_ok() {  # $1=port -> prints "ok" or "FAIL:<reason>"
  "$PY" - "$1" <<'PYEOF'
import json, sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen(
            "http://127.0.0.1:%s/healthz" % port, timeout=3) as r:
        body = json.loads(r.read().decode("utf-8"))
        if r.status != 200 or body.get("status") != "ok":
            print("FAIL: status=%s body=%r" % (r.status, body))
        else:
            print("ok")
except Exception as exc:  # noqa: BLE001 - any failure is a smoke failure
    print("FAIL:%s" % exc)
PYEOF
}

kill_pids() {  # kill everything in $PIDS: TERM, wait, KILL
  [ "$CLEANED" -eq 1 ] && return 0
  CLEANED=1
  while IFS='|' read -r port pid dir; do
    [ -n "${pid:-}" ] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done < "$PIDS"
  i=0
  while [ "$i" -lt 20 ]; do
    alive=0
    while IFS='|' read -r port pid dir; do
      [ -n "${pid:-}" ] || continue
      if kill -0 "$pid" >/dev/null 2>&1; then alive=1; fi
    done < "$PIDS"
    [ "$alive" -eq 0 ] && break
    i=$((i + 1)); sleep 0.5
  done
  while IFS='|' read -r port pid dir; do
    [ -n "${pid:-}" ] || continue
    kill -9 "$pid" >/dev/null 2>&1 || true
  done < "$PIDS"
}

on_exit() {  # $1 = desired exit code (may be $? when unset)
  kill_pids
  rm -rf "$WORK" 2>/dev/null
  trap - EXIT INT TERM
  exit "$1"
}
trap 'on_exit $?' EXIT
trap 'on_exit 130' INT
trap 'on_exit 143' TERM

# --- 0. pre-flight: all 15 ports must be free --------------------------------
busy=""
while IFS='|' read -r port kind dir pkg extra; do
  if port_open "$port"; then busy="$busy $port"; fi
done < "$TABLEF"
if [ -n "$busy" ]; then
  echo "smoke: ports already in use:$busy — run kill-all.sh first"
  on_exit 1
fi

# --- 1. boot all 15 -----------------------------------------------------------
echo "== booting 15 services"
while IFS='|' read -r port kind dir pkg extra; do
  log="$WORK/$dir.log"
  case "$kind" in
    py)
      ( cd "$ROOT/services/$dir" && PYTHONPATH=src "$PY" -m "$pkg.main" $extra \
          > "$log" 2>&1 & echo $! > "$WORK/pid.$dir" )
      ;;
    rs)
      bin="$ROOT/services/$dir/target/debug/$dir"
      if [ ! -x "$bin" ]; then
        echo "== building $dir (no debug binary)"
        ( cd "$ROOT/services/$dir" && cargo build --message-format=short ) \
          > "$log" 2>&1 || { echo "   build FAILED: $dir (see $log)"; on_exit 1; }
      fi
      ( cd "$ROOT/services/$dir" && "$bin" $extra > "$log" 2>&1 & \
        echo $! > "$WORK/pid.$dir" )
      ;;
  esac
  i=0
  while [ ! -s "$WORK/pid.$dir" ] && [ "$i" -lt 50 ]; do
    i=$((i + 1)); sleep 0.1
  done
  pid="$(cat "$WORK/pid.$dir" 2>/dev/null || echo 0)"
  if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    echo "   FAILED to launch: $dir"
    on_exit 1
  fi
  echo "$port|$pid|$dir" >> "$PIDS"
  echo "   up: $dir (pid $pid)"
done < "$TABLEF"

# --- 2. wait for all 15 ports --------------------------------------------------
i=0
while :; do
  up=0
  while IFS='|' read -r port kind dir pkg extra; do
    if port_open "$port"; then up=$((up + 1)); fi
  done < "$TABLEF"
  [ "$up" -eq 15 ] && break
  i=$((i + 1))
  if [ "$i" -ge 60 ]; then
    echo "smoke: only $up/15 ports accepting connections after 30s"
    while IFS='|' read -r port kind dir pkg extra; do
      if ! port_open "$port"; then
        echo "  port :$port NOT up — $dir"
        tail -5 "$WORK/$dir.log"
      fi
    done < "$TABLEF"
    on_exit 1
  fi
  sleep 0.5
done
echo "== all 15 ports accepting connections"

# --- 3. verify /healthz on every port ------------------------------------------
echo "== healthz sweep"
fail=0
while IFS='|' read -r port kind dir pkg extra; do
  out="$(healthz_ok "$port")"
  case "$out" in
    ok) echo "  :$port $dir  ok" ;;
    *)  echo "  :$port $dir  $out"; fail=1 ;;
  esac
done < "$TABLEF"

# --- 4. kill all + confirm every port freed ------------------------------------
echo "== shutting down 15 services"
kill_pids
freed=0
while IFS='|' read -r port kind dir pkg extra; do
  if port_open "$port"; then
    echo "  port :$port STILL in use after kill"
    fail=1
  else
    freed=$((freed + 1))
  fi
done < "$TABLEF"
echo "  ports freed: $freed/15"

echo "-------------------------------------------------------------"
if [ "$fail" -ne 0 ]; then
  echo "smoke: FAIL"
else
  echo "smoke: PASS — 15/15 booted, healthz ok, 15/15 killed, all ports free"
fi
CLEANED=1
rm -rf "$WORK" 2>/dev/null
trap - EXIT INT TERM
exit "$fail"
