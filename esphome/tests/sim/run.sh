#!/bin/bash
# Build and run the host simulation tests (see sim_test.py).
#
#   bash esphome/tests/sim/run.sh [variant...]     variants: plain asan tsan
#
# Without arguments runs "plain" and "asan" (ASan + UBSan).  Variants run in
# parallel on different TCP ports.  "tsan" is optional and not run by default;
# on kernels with a high ASLR entropy it needs `setarch -R` (used if present).
# Requires g++ (C++20) and python3; nothing outside the standard library.
set -euo pipefail

sim_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
variants=("$@")
if [ ${#variants[@]} -eq 0 ]; then
  variants=(plain asan)
fi

# Build everything first so that compile errors are reported before any test.
declare -A binaries
for v in "${variants[@]}"; do
  echo "== building $v"
  binaries[$v]="$(bash "$sim_dir/build_sim.sh" "$v" | tail -n 1)"
done

pids=()
logs=()
port=19900
for v in "${variants[@]}"; do
  cmd="${binaries[$v]}"
  if [ "$v" = tsan ] && command -v setarch > /dev/null; then
    cmd="setarch $(uname -m) -R $cmd"
  fi
  log="$(mktemp)"
  logs+=("$log")
  echo "== running $v on port $port"
  SIM_PORT=$port ASAN_OPTIONS="${ASAN_OPTIONS:-detect_leaks=0}" \
    python3 "$sim_dir/sim_test.py" "$cmd" > "$log" 2>&1 &
  pids+=($!)
  port=$((port + 1))
done

status=0
for i in "${!pids[@]}"; do
  if wait "${pids[$i]}"; then
    result=passed
  else
    result=FAILED
    status=1
  fi
  echo
  echo "===== ${variants[$i]}: $result ====="
  cat "${logs[$i]}"
  rm -f "${logs[$i]}"
done
exit $status
