#!/bin/bash
# Build the host simulation of the sma_bt_bridge firmware.
#
#   bash build_sim.sh [plain|asan|tsan]
#
# Compiles the real esphome/components/sma_bt_bridge/sma_bt_bridge.cpp against
# the stub headers in stubs/ together with sim_main.cpp (a fake Bluedroid and a
# scriptable inverter).  Prints the path of the binary on the last line.
# Output goes to $SIM_OUT_DIR (default: $TMPDIR or /tmp), never into the tree.
set -euo pipefail

variant="${1:-plain}"
sim_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
component_dir="$sim_dir/../../components/sma_bt_bridge"
out_dir="${SIM_OUT_DIR:-${TMPDIR:-/tmp}}"
out="$out_dir/sma_bt_bridge_sim_$variant"

case "$variant" in
  plain) sanitize=() ;;
  asan) sanitize=(-fsanitize=address,undefined -fno-omit-frame-pointer) ;;
  tsan) sanitize=(-fsanitize=thread) ;;
  *)
    echo "unknown variant '$variant' (plain|asan|tsan)" >&2
    exit 2
    ;;
esac

mkdir -p "$out_dir"
# gnu++20 is what ESPHome compiles with (std::span in the socket API).
g++ -std=gnu++20 -g -O1 -Wall -Wextra -Wno-unused-parameter "${sanitize[@]}" \
  -I"$sim_dir/stubs" -I"$component_dir" \
  "$component_dir/sma_bt_bridge.cpp" "$sim_dir/sim_main.cpp" \
  -o "$out" -pthread

echo "$out"
