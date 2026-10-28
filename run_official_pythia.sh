#!/usr/bin/env bash
set -Eeuo pipefail
if (( $# < 5 )); then
 echo 'Usage: ./run_official_pythia.sh pythia|none BINARY WARMUP_INSTR SIM_INSTR TRACE0 [TRACE1 ...]' >&2; exit 2
fi
root="$(cd "$(dirname "$0")" && pwd -P)"
mode="$1"; binary="$2"; warm="$3"; sim="$4"; shift 4
case "$mode" in
 pythia) args=("--l2c_prefetcher_types=scooby" "--config=$root/config/pythia.ini") ;;
 none) args=("--config=$root/config/nopref.ini") ;;
 *) echo 'Mode must be pythia or none' >&2; exit 2 ;;
esac
[[ "$warm" =~ ^[0-9]+$ && "$sim" =~ ^[0-9]+$ ]] || { echo 'Invalid instruction counts' >&2; exit 2; }
[[ "$binary" = /* ]] || binary="$root/$binary"
[[ -x "$binary" ]] || { echo "Binary not executable: $binary" >&2; exit 2; }
[[ "$(basename "$binary")" =~ -([0-9]+)core$ ]] || { echo 'Binary must end -Ncore' >&2; exit 2; }
ncores="${BASH_REMATCH[1]}"
(( $# == ncores )) || { echo "Expected $ncores traces; received $#" >&2; exit 2; }
traces=()
for t in "$@"; do [[ "$t" = /* ]] || t="$root/$t"; [[ -f "$t" ]] || { echo "Missing trace: $t" >&2; exit 2; }; traces+=("$t"); done
cd "$root"
exec "$binary" "--warmup_instructions=$warm" "--simulation_instructions=$sim" "${args[@]}" -traces "${traces[@]}"
