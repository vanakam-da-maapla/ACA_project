#!/bin/bash
ROOT=~/ACA_verify
TD=~/ACA_project/dpc3_traces
WARM=${WARM:-1000000}
SIM=${SIM:-5000000}
JOBS=${JOBS:-$(( $(sysctl -n hw.ncpu) / 2 ))}
[ "$JOBS" -lt 1 ] && JOBS=1
OUT=$ROOT/results_all
mkdir -p $OUT $OUT/jobs

# ---- 1. build missing binaries (sequential: build script edits champsim.h) ----
build() { # tree bin args...
  t=$1; b=$2; shift 2
  if [ ! -f $ROOT/$t/bin/$b ]; then
    echo "Building $b in $t"
    (cd $ROOT/$t && ./build_champsim.sh "$@" 2>&1 | grep -E "FAILED|successfully")
  fi
}
build ipcp bimodal-no-no-no-no-lru-4core bimodal no no no no lru 4
build ipcp bimodal-no-no-no-no-ucp-4core bimodal no no no no ucp 4
build ipcp bimodal-no-ipcp_isca2020-ipcp_isca2020-no-lru-4core bimodal no ipcp_isca2020 ipcp_isca2020 no lru 4
build ipcp bimodal-no-ipcp_isca2020-ipcp_isca2020-no-ucp-4core bimodal no ipcp_isca2020 ipcp_isca2020 no ucp 4
build w_ucp_pythia bimodal-no-no-pythia-no-lru-4core bimodal no no pythia no lru 4
build w_ucp_pythia bimodal-no-no-pythia-no-ucp-4core bimodal no no pythia no ucp 4

# ---- 2. make mixes: every trace used, 4 per mix (wraps to fill last mix) ----
ls $TD/*.champsimtrace.xz | sort > $OUT/traces.txt
N=$(wc -l < $OUT/traces.txt)
NM=$(( (N + 3) / 4 ))
: > $OUT/mixes.txt
for ((m=0; m<NM; m++)); do
  line=""
  for ((k=0; k<4; k++)); do
    i=$(( (m*4 + k) % N + 1 ))
    line="$line $(sed -n "${i}p" $OUT/traces.txt)"
  done
  echo "mix$m$line" >> $OUT/mixes.txt
done
echo "$N traces -> $NM mixes, warm=$WARM sim=$SIM, parallel jobs=$JOBS"

# ---- 3. one job script per (mix, config) ----
CONFIGS="base_lru:ipcp:bimodal-no-no-no-no-lru-4core
ucp_only:ipcp:bimodal-no-no-no-no-ucp-4core
ipcp_lru:ipcp:bimodal-no-ipcp_isca2020-ipcp_isca2020-no-lru-4core
ucp_ipcp:ipcp:bimodal-no-ipcp_isca2020-ipcp_isca2020-no-ucp-4core
pythia_lru:w_ucp_pythia:bimodal-no-no-pythia-no-lru-4core
ucp_pythia:w_ucp_pythia:bimodal-no-no-pythia-no-ucp-4core"
rm -f $OUT/jobs/*.sh
while read -r mix t1 t2 t3 t4; do
  echo "$CONFIGS" | while IFS=: read -r name tree bin; do
    res=$OUT/${mix}_${name}.txt
    cat > $OUT/jobs/${mix}_${name}.sh << JOB
#!/bin/bash
grep -q "ChampSim completed all CPUs" $res 2>/dev/null && exit 0
cd $ROOT/$tree && ./bin/$bin -warmup_instructions $WARM -simulation_instructions $SIM -traces $t1 $t2 $t3 $t4 > $res 2>&1
JOB
  done
done < $OUT/mixes.txt

ls $OUT/jobs/*.sh | xargs -P $JOBS -n1 bash
echo "All runs done. Summarizing..."

# ---- 4. summary ----
cat > $OUT/summarize.py << 'PY'
import re, os, math, sys
out = sys.argv[1]
cfgs = ["base_lru","ucp_only","ipcp_lru","ucp_ipcp","pythia_lru","ucp_pythia"]
mixes = [l.split()[0] for l in open(out+"/mixes.txt")]
def ipcs(f):
    try: v = re.findall(r"^CPU \d cumulative IPC: ([\d.]+)", open(f).read(), re.M)
    except: return None
    return [float(x) for x in v[-4:]] if len(v) >= 4 else None
rows, norm = [], {c: [] for c in cfgs}
for m in mixes:
    r = {c: ipcs(f"{out}/{m}_{c}.txt") for c in cfgs}
    base = r["base_lru"]
    row = [m]
    for c in cfgs:
        if r[c] and base:
            n = sum(a/b for a, b in zip(r[c], base))/4
            norm[c].append(n)
            row += [round(sum(r[c]), 4), round(n, 4)]
        else: row += ["", ""]
    rows.append(row)
hdr = ["mix"] + [f"{c}_{k}" for c in cfgs for k in ("sumIPC", "normIPC")]
with open(out+"/summary.csv", "w") as f:
    f.write(",".join(hdr)+"\n")
    for r in rows: f.write(",".join(map(str, r))+"\n")
print("\nGeomean of mean normalized IPC (vs base_lru) over", len(norm["base_lru"]), "mixes")
for c in cfgs:
    v = norm[c]
    print(f"{c:12s} {math.exp(sum(map(math.log, v))/len(v)):.4f}" if v else f"{c:12s} n/a")
print("\nPer-mix table: results_all/summary.csv")
PY
python3 $OUT/summarize.py $OUT
