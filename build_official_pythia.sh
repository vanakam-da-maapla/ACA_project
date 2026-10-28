#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
if (( $# < 1 || $# > 3 )); then echo 'Usage: ./build_official_pythia.sh CORES [ship|lru|srrip|drrip] [perceptron|bimodal]' >&2; exit 2; fi
cores="$1"; repl="${2:-ship}"; branch="${3:-perceptron}"
[[ "$cores" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid core count' >&2; exit 2; }
[[ -f "replacement/$repl.llc_repl" && -f "branch/$branch.bpred" ]] || { echo 'Unknown replacement policy or branch predictor' >&2; exit 2; }
if [[ ! -f libbf/build/lib/libbf.a ]]; then
 cat >&2 <<'TEXT'
Official reference dependency missing. Run once:
  git clone https://github.com/mavam/libbf.git libbf
  cmake -S libbf -B libbf/build
  cmake --build libbf/build -j
Then rerun the build. The libbf directory is Git-ignored.
TEXT
 exit 3
fi
PYTHIA_LLC_REPLACEMENT="$repl" PYTHIA_BRANCH_PRED="$branch" TERM="${TERM:-xterm}" ./build_champsim.sh multi pythia no "$cores"
echo "Binary: bin/$branch-multi-pythia-no-$repl-${cores}core"
