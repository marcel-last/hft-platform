#!/bin/sh
# =============================================================================
# kill-all.sh — stop every running HFT platform service, then confirm all 15
# ports (7610-7750) are freed.
#
# POSIX sh compatible. Does NOT rely on pgrep/lsof/ss (not present in this
# environment): it scans /proc/<pid>/cmdline for the known service tokens and
# kills matches (SIGTERM, then SIGKILL on laggards). Port verification uses
# python's socket.create_connection.
#
# Usage: scripts/kill-all.sh [ports...]   (default: all 15 platform ports)
# =============================================================================
set -u

if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi

# Tokens that identify a platform service in its cmdline.
#   Python services appear as:  python -m <pkg>.main ...
#   Rust services appear as:    .../target/debug/<binary> ...
PY_TOKENS="mdg.main obb.main ste.main rkm.main posk.main dqm.main pfa.main cfgs.main audl.main stls.main"
RS_BINARIES="execution-gateway latency-monitor alerting-service auth-service api-gateway"

# Default port set; overridable via arguments.
if [ "$#" -gt 0 ]; then
  PORTS="$*"
else
  PORTS="7610 7620 7630 7640 7650 7660 7670 7680 7690 7700 7710 7720 7730 7740 7750"
fi

port_open() {
  "$PY" -c "import socket,sys
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[1])), 1).close()
except OSError:
    sys.exit(1)" "$1" >/dev/null 2>&1
}

is_rs_binary() {  # $1=basename -> 0 if one of our Rust binaries
  for b in $RS_BINARIES; do
    [ "$1" = "$b" ] && return 0
  done
  return 1
}

# --- 1. collect candidate pids -------------------------------------------------
candidates=""
for d in /proc/[0-9]*; do
  pid="${d#/proc/}"
  [ "$pid" = "$$" ] && continue
  cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
  [ -n "$cmd" ] || continue
  # first executable token (strip path)
  first="${cmd%% *}"
  base="${first##*/}"
  case "$base" in
    python|python3|python3.*)
      for tok in $PY_TOKENS; do
        case "$cmd" in
          *"$tok"*) candidates="$candidates $pid"; break ;;
        esac
      done
      ;;
    *)
      if is_rs_binary "$base"; then
        candidates="$candidates $pid"
      fi
      ;;
  esac
done

# de-duplicate + drop self
candidates=$(printf '%s\n' $candidates | sort -un | grep -vx "$$" || true)

# --- 2. report + kill -----------------------------------------------------------
if [ -z "$candidates" ]; then
  echo "kill-all: no platform service processes found"
else
  echo "kill-all: found $(echo $candidates | wc -w | tr -d ' ') process(es):"
  for pid in $candidates; do
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
    echo "  pid $pid: $cmd"
  done
  echo "  sending SIGTERM ..."
  for pid in $candidates; do kill "$pid" 2>/dev/null || true; done
  i=0
  while [ "$i" -lt 20 ]; do
    alive=0
    for pid in $candidates; do
      kill -0 "$pid" >/dev/null 2>&1 && alive=1
    done
    [ "$alive" -eq 0 ] && break
    i=$((i + 1)); sleep 0.5
  done
  # force-kill any laggards
  for pid in $candidates; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      echo "  pid $pid still alive — SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
fi

# --- 3. confirm every requested port is freed ----------------------------------
echo "  checking ports ..."
fail=0
for port in $PORTS; do
  if port_open "$port"; then
    echo "  :$port STILL in use"
    fail=1
  fi
done
if [ "$fail" -ne 0 ]; then
  echo "kill-all: some ports remain in use (manual check needed)"
else
  echo "kill-all: all $(echo $PORTS | wc -w | tr -d ' ') ports free"
fi
exit "$fail"
