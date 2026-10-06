#!/bin/bash
# ============================================================================
# scripts/characterize_workloads.sh
#
# LLC-P4: single-core characterization sweep.
# Builds a 2D response surface -- performance/bandwidth/occupancy as a
# function of (LLC ways, prefetch degree) -- for each trace listed below.
#
# NOTE: LLC_WAY and the prefetcher's degree constant are compile-time
# #defines in this ChampSim variant, so each (ways, degree) point requires
# a full rebuild. This script automates that build+run loop.
#
# Place this file at:  <repo_root>/scripts/characterize_workloads.sh
# Run it from the repo root or anywhere: bash scripts/characterize_workloads.sh
# ============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Resolve paths relative to this script's location, so it works regardless
# of the caller's current working directory.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

CACHE_HEADER="$ROOT_DIR/inc/cache.h"
PREFETCHER_FILE="$ROOT_DIR/prefetcher/ip_stride.l2c_pref"
RUN_SCRIPT="$ROOT_DIR/run_champsim.sh"
TRACE_DIR="$ROOT_DIR/dpc3_traces"

# ---------------------------------------------------------------------------
# 1) WORKLOAD / TRACE LIST  (actual filenames from dpc3_traces/)
#    Add or remove entries here as needed.
# ---------------------------------------------------------------------------
TRACES=(
  "401.bzip2-277B.champsimtrace.xz"
  "403.gcc-16B.champsimtrace.xz"
  "434.zeusmp-10B.champsimtrace.xz"
  "437.leslie3d-273B.champsimtrace.xz"
  "450.soplex-92B.champsimtrace.xz"
  "456.hmmer-327B.champsimtrace.xz"
  "462.libquantum-1343B.champsimtrace.xz"
  "482.sphinx3-1522B.champsimtrace.xz"
  "605.mcf_s-665B.champsimtrace.xz"
  "605.mcf_s-1644B.champsimtrace.xz"
  "619.lbm_s-3766B.champsimtrace.xz"
  "620.omnetpp_s-874B.champsimtrace.xz"
  "621.wrf_s-8100B.champsimtrace.xz"
  "623.xalancbmk_s-700B.champsimtrace.xz"
  "628.pop2_s-17B.champsimtrace.xz"
)

# ---------------------------------------------------------------------------
# 2) SWEEP PARAMETERS (the two knobs, per the problem statement)
# ---------------------------------------------------------------------------
# LLC_WAYS_LIST=(1 2 4 8 16 32)
LLC_WAYS_LIST=(4 8 16 32)
PREFETCH_DEGREES=(1 2 4 6 8)     # 0 = prefetcher fully disabled (no.l2c_pref)

# Number of trace simulation jobs to run concurrently for each built binary.
# Override via CLI environment variable, e.g.: PARALLEL_JOBS=8 bash scripts/characterize_workloads.sh
PARALLEL_JOBS="${PARALLEL_JOBS:-15}"

# Warmup / detailed simulation instruction counts, in MILLIONS.
# run_champsim.sh appends "000000" itself -- pass plain integers here.
N_WARM=50
N_SIM=250

DEFAULT_RESULTS_DIR="$ROOT_DIR/results_${N_SIM}M"
RESULTS_DIR="$ROOT_DIR/results_${N_SIM}M/characterization"
mkdir -p "$RESULTS_DIR"

# ---------------------------------------------------------------------------
# 3) FIXED CONFIG for this study (everything NOT being swept)
#    Single core, per the characterization phase's requirement.
# ---------------------------------------------------------------------------
BRANCH_PRED="bimodal"
L1I_PREF="no"            # L1I prefetching held off for this study
L1D_PREF="no"
LLC_PREF="no"           # keep LLC-level prefetch off; all prefetch effects
                         # come from the L2C-level knob being swept
REPL="srrip"              # primary replacement policy
N_CORES=1                 # single-core characterization

# ---------------------------------------------------------------------------
# 4) BEFORE FIRST RUN -- verify macros
# ---------------------------------------------------------------------------
PREFETCH_DEGREE_MACRO="PREFETCH_DEGREE"

# Backup original files so they can be restored after the sweep
cp "$CACHE_HEADER" "${CACHE_HEADER}.orig"
cp "$PREFETCHER_FILE" "${PREFETCHER_FILE}.orig"

restore_originals () {
  echo "Restoring original cache.h and prefetcher source..."
  cp "${CACHE_HEADER}.orig" "$CACHE_HEADER"
  cp "${PREFETCHER_FILE}.orig" "$PREFETCHER_FILE"
}
trap restore_originals EXIT

set_llc_ways () {
  local ways=$1
  sed -i -E "s/#define LLC_WAY [0-9]+/#define LLC_WAY ${ways}/" "$CACHE_HEADER"
}

set_prefetch_degree () {
  local degree=$1
  sed -i -E "s/#define ${PREFETCH_DEGREE_MACRO} [0-9]+/#define ${PREFETCH_DEGREE_MACRO} ${degree}/" "$PREFETCHER_FILE"
}

# ---------------------------------------------------------------------------
# 5) MAIN SWEEP
#    For every (ways, degree) pair: patch configs -> rebuild -> run traces
#    in parallel -> move+rename results.
# ---------------------------------------------------------------------------
total_configs=$(( ${#LLC_WAYS_LIST[@]} * ${#PREFETCH_DEGREES[@]} ))
config_num=0

echo "Starting sweep with PARALLEL_JOBS=${PARALLEL_JOBS}"

for ways in "${LLC_WAYS_LIST[@]}"; do
  set_llc_ways "$ways"

  for degree in "${PREFETCH_DEGREES[@]}"; do
    config_num=$((config_num + 1))

    if [ "$degree" -eq 0 ]; then
      L2C_PREF="no"           # prefetcher disabled entirely for this point
    else
      L2C_PREF="ip_stride"
      set_prefetch_degree "$degree"
    fi

    echo "=== [$config_num/$total_configs] Building: LLC_WAY=${ways}  L2C_PREF=${L2C_PREF}  DEGREE=${degree} ==="
    cd "$ROOT_DIR"
    ./build_champsim.sh "$BRANCH_PRED" "$L1I_PREF" "$L1D_PREF" "$L2C_PREF" "$LLC_PREF" "$REPL" "$N_CORES"

    BINARY_NAME="${BRANCH_PRED}-${L1I_PREF}-${L1D_PREF}-${L2C_PREF}-${LLC_PREF}-${REPL}-${N_CORES}core"

    echo "    -> Executing ${#TRACES[@]} traces using up to ${PARALLEL_JOBS} parallel workers..."

    # Pipe all traces to xargs to execute simulation jobs concurrently
    printf "%s\n" "${TRACES[@]}" | xargs -P "$PARALLEL_JOBS" -I {} bash -c '
      trace="$1"
      res_dir="$2"
      ways="$3"
      deg="$4"
      run_script="$5"
      binary="$6"
      n_warm="$7"
      n_sim="$8"
      def_res_dir="$9"

      trace_name="${trace%.champsimtrace.xz}"
      final_out="${res_dir}/${trace_name}_way${ways}_deg${deg}.txt"

      echo "        [START] ${trace_name}"
      "$run_script" "$binary" "$n_warm" "$n_sim" "$trace" > /dev/null 2>&1

      default_out="${def_res_dir}/${trace}-${binary}.txt"
      if [ -f "$default_out" ]; then
        mv "$default_out" "$final_out"
        echo "        [DONE]  ${trace_name}"
      else
        echo "        [WARN]  expected output not found for ${trace_name}"
      fi
    ' _ {} "$RESULTS_DIR" "$ways" "$degree" "$RUN_SCRIPT" "$BINARY_NAME" "$N_WARM" "$N_SIM" "$DEFAULT_RESULTS_DIR"

  done
done

echo ""
echo "Characterization sweep complete."
echo "Total configurations: $total_configs   |   Total runs: $((total_configs * ${#TRACES[@]}))"
echo "Results saved in: $RESULTS_DIR"