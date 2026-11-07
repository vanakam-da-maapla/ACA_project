#!/usr/bin/env python3
"""
run_4core_mixes.py  --  50 four-core workload mixes, run in parallel
===============================================================================
What it does
  1. Builds 50 four-core mixes from the 64 single-core traces of the
     "IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx" workbook (tab "Tacker Segregation").
     Each trace belongs to one of four classes:

         HCHP = High-cache / High-prefetch     HCLP = High-cache / Low-prefetch
         LCHP = Low-cache  / High-prefetch     LCLP = Low-cache  / Low-prefetch

     ("cache" = IPC gain from more LLC ways, "prefetch" = IPC gain from a more
      aggressive prefetcher; High/Low is split at the median, as in the workbook.)

  2. The 50 mixes cover every possible class composition of 4 cores:

         type                 mixes   example
         homogeneous            8     4xHCHP            (2 mixes per class)
         two-class 2+2         12     2xHCHP + 2xLCLP   (6 class pairs x 2)
         two-class 3+1         12     3xHCLP + 1xLCHP   (12 ordered class pairs)
         three-class 2+1+1     12     2xHCHP + 1xHCLP + 1xLCLP
         all four classes       6     1xHCHP + 1xHCLP + 1xLCHP + 1xLCLP

     Inside a mix: 4 different programs (no two traces of the same benchmark
     family, e.g. 429.mcf and 605.mcf_s count as one family), every trace is used
     a similar number of times over the 50 mixes, and no pair of traces repeats
     more than necessary.  Only CLEAR representatives of a class are used: traces
     whose cache or prefetch benefit is within a factor of --margin (default 1.75)
     of the median are "borderline" and are left out.  The selection is
     deterministic (--seed), so every binary you test sees the same 50 mixes.

  3. Runs each mix with 50M warm-up + 200M simulated instructions per core
     on a 4-core ChampSim whose shared LLC is 2048 sets x 16 ways (= 2 MB).
     NOTE: with the default Pythia-base sources a 4-core build gets 2048 sets
     PER CORE (8192 sets, 8 MB).  Use --build to create a binary with the fixed
     2048 x 16 LLC, and the script verifies the geometry of whatever binary you
     run (before starting, and again in every log) -- it refuses to run a
     binary with a different LLC.

  4. Parallelism: set PARALLEL_JOBS in the USER SETTINGS block below (or --jobs N / env PARALLEL_JOBS)
     to run that many 4-core simulations at the same time.

  5. Every simulation gets its OWN folder, grouped by experiment:

         results_4core_mixes/<experiment>/            e.g.  ucp   |   ucp_pythia
             mix01_4xHCHP/mix01_4xHCHP.txt            full simulator output (log)
             mix01_4xHCHP/command.txt, status.txt     exact command line, result and run time
             ...  (one folder per mix)
             mixes.csv  summary.csv  run_manifest.txt  (mixes, per-core IPC/LLC misses, run details)

     Finished mixes are skipped when you re-run (--force redoes them).

Consistent setup across experiments (same binary, same LLC 2048 x 16, same mixes)
    UCP only        python3 scripts/run_4core_mixes.py --repl ucp --mode none
    UCP + Pythia    python3 scripts/run_4core_mixes.py --repl ucp --mode pythia
    Pythia only     python3 scripts/run_4core_mixes.py --repl ship --build --mode pythia   (baseline policy of your choice)
  UCP-only and UCP+Pythia use the SAME binary (bin/..-ucp-4core-llc2048s16w); only the prefetcher
  configuration differs.  After running experiments, check that their simulator setups match:
    python3 scripts/run_4core_mixes.py --compare-setups results_4core_mixes/ucp results_4core_mixes/ucp_pythia

Quick start (from the repo root, or anywhere)
    python3 scripts/run_4core_mixes.py --list                      # show the 50 mixes
    python3 scripts/run_4core_mixes.py --build --build-only        # build the UCP binary, LLC 2048 x 16
    python3 scripts/run_4core_mixes.py --repl ucp --mode none --jobs 8
    python3 scripts/run_4core_mixes.py --repl ucp --mode pythia --jobs 8
    python3 scripts/run_4core_mixes.py --only 1-6,40               # run a subset
    python3 scripts/run_4core_mixes.py --summarize-only            # rebuild summary.csv from the logs

Only the Python standard library is needed (openpyxl only for --classes-xlsx).
===============================================================================
"""
import argparse
import csv
import hashlib
import itertools
import os
import random
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ============================================================================
#  USER SETTINGS -- edit here, or override on the command line
# ============================================================================
PARALLEL_JOBS = 4                      # number of 4-core simulations running at the same time
#                                        precedence:  --jobs  >  env PARALLEL_JOBS  >  this value
WARMUP_M = 50                          # warm-up instructions per core, in millions
SIM_M = 200                            # simulated instructions per core, in millions
LLC_SETS = 2048                        # required LLC sets  (one LLC shared by the 4 cores)
LLC_WAYS = 16                          # required LLC ways
RESULTS_ROOT = "results_4core_mixes"   # under the repo root; one sub-folder per experiment
# ============================================================================

NCORES = 4
CLASSES = ["HCHP", "HCLP", "LCHP", "LCLP"]
CLASS_TEXT = {"HCHP": "High-cache / High-prefetch", "HCLP": "High-cache / Low-prefetch",
              "LCHP": "Low-cache / High-prefetch", "LCLP": "Low-cache / Low-prefetch"}
LABEL_TO_CLASS = {v: k for k, v in CLASS_TEXT.items()}

# --------------------------------------------------------------------------
# Trace classification, copied from the workbook's "Tacker Segregation" tab:
#   trace, cache benefit, prefetch benefit, class
# Medians used by the workbook to split High / Low (cells B5, B6):
# --------------------------------------------------------------------------
MEDIAN_CACHE = 0.0256772022150327
MEDIAN_PREFETCH = 0.026811097294712934
EMBEDDED_TABLE = """400.perlbench-41B,0.008957,0.002870,LCLP
401.bzip2-277B,0.131737,0.023282,HCLP
403.gcc-16B,0.444346,0.001129,HCLP
403.gcc-17B,0.414923,0.014640,HCLP
410.bwaves-945B,0.000004,0.000222,LCLP
410.bwaves-2097B,0.000893,0.285677,LCHP
416.gamess-875B,0.000000,0.000095,LCLP
429.mcf-184B,0.147232,0.000043,HCLP
429.mcf-192B,0.215040,-0.000008,HCLP
434.zeusmp-10B,0.023309,0.031636,LCHP
437.leslie3d-273B,0.012918,0.283142,LCHP
450.soplex-92B,0.177058,0.138083,HCHP
454.calculix-104B,0.000000,0.000115,LCLP
454.calculix-460B,0.113499,0.056229,HCHP
456.hmmer-191B,0.171120,0.002794,HCLP
456.hmmer-327B,0.125605,0.003089,HCLP
459.GemsFDTD-765B,0.017598,0.050958,LCHP
459.GemsFDTD-1320B,-0.009040,0.305316,LCHP
459.GemsFDTD-1418B,0.007351,0.163107,LCHP
462.libquantum-714B,-0.004474,0.492441,LCHP
462.libquantum-1343B,-0.031138,0.629602,LCHP
464.h264ref-30B,0.022908,0.016700,LCLP
464.h264ref-57B,0.023655,0.018101,LCLP
464.h264ref-97B,0.026461,0.019631,HCLP
465.tonto-44B,0.002735,0.000119,LCLP
465.tonto-1914B,0.008664,0.000417,LCLP
470.lbm-1274B,-0.021119,0.033514,LCHP
471.omnetpp-188B,0.104849,0.001957,HCLP
473.astar-42B,0.246458,0.002414,HCLP
473.astar-153B,0.024894,0.004753,LCLP
473.astar-359B,0.053933,0.011954,HCLP
481.wrf-196B,0.006071,0.155370,LCHP
481.wrf-1170B,0.001011,0.001318,LCLP
481.wrf-1254B,0.020144,0.178812,LCHP
481.wrf-1281B,-0.000373,0.220215,LCHP
482.sphinx3-234B,0.075041,0.223248,HCHP
482.sphinx3-1100B,0.136735,0.234222,HCHP
482.sphinx3-1522B,0.152370,0.217774,HCHP
483.xalancbmk-736B,0.030032,0.007272,HCLP
600.perlbench_s-210B,0.000300,0.000348,LCLP
600.perlbench_s-570B,0.034984,0.003629,HCLP
602.gcc_s-734B,0.004240,0.297141,LCHP
602.gcc_s-1850B,0.001525,0.410260,LCHP
602.gcc_s-2226B,-0.000481,1.095990,LCHP
602.gcc_s-2375B,0.095159,0.005042,HCLP
603.bwaves_s-1740B,-0.000530,0.145733,LCHP
603.bwaves_s-2609B,-0.000542,0.143833,LCHP
605.mcf_s-665B,0.385687,0.072232,HCHP
605.mcf_s-1152B,0.679446,0.111996,HCHP
605.mcf_s-1536B,0.035871,-0.004064,HCLP
605.mcf_s-1644B,0.167178,0.119181,HCHP
607.cactuBSSN_s-2421B,0.027203,-0.103928,HCLP
619.lbm_s-2676B,-0.033383,0.036984,LCHP
619.lbm_s-3766B,-0.041541,0.030340,LCHP
620.omnetpp_s-141B,0.066638,0.001724,HCLP
620.omnetpp_s-874B,0.063811,0.002273,HCLP
621.wrf_s-6673B,0.082062,0.206186,HCHP
621.wrf_s-8100B,0.010772,0.004907,LCLP
623.xalancbmk_s-10B,0.223645,0.004473,HCLP
623.xalancbmk_s-700B,0.077545,0.002882,HCLP
627.cam4_s-490B,0.147518,0.157984,HCHP
628.pop2_s-17B,0.108669,0.080362,HCHP
649.fotonik3d_s-1176B,0.000509,0.710079,LCHP
654.roms_s-293B,0.088781,0.109533,HCHP"""

Trace = namedtuple("Trace", "name cache pref cls")
Mix = namedtuple("Mix", "id name mtype classes traces")   # classes/traces: tuples of length 4


def embedded_traces():
    out = []
    for line in EMBEDDED_TABLE.strip().splitlines():
        n, c, p, k = line.split(",")
        out.append(Trace(n, float(c), float(p), k))
    return out


def load_traces_from_xlsx(path):
    """Read classes + medians from a workbook produced by make_characterization_excel.py
    (needs openpyxl, and the workbook must contain cached values, i.e. have been
    opened/saved once by Excel or LibreOffice)."""
    from openpyxl import load_workbook
    ws = load_workbook(path, data_only=True)["Tacker Segregation"]
    med_c, med_p = ws["B5"].value, ws["B6"].value
    if not isinstance(med_c, (int, float)) or not isinstance(med_p, (int, float)):
        sys.exit("[ERROR] %s has no cached values (open it in Excel and save it once)." % path)
    out = []
    for r in range(9, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if a and "Segregated" in str(a):
            break
        cls = LABEL_TO_CLASS.get(ws.cell(r, 9).value)
        if a and cls:
            out.append(Trace(a, float(ws.cell(r, 5).value), float(ws.cell(r, 8).value), cls))
    if not out:
        sys.exit("[ERROR] no classified traces found in %s" % path)
    return out, float(med_c), float(med_p)


def family(trace):
    """'605.mcf_s-665B' -> 'mcf' ; '429.mcf-184B' -> 'mcf' ; '602.gcc_s-734B' -> 'gcc'"""
    bench = trace.rsplit("-", 1)[0]
    return re.sub(r"_s$", "", bench.split(".", 1)[-1]).lower()


# --------------------------------------------------------------------------
# Mix generation
# --------------------------------------------------------------------------
def clear_pools(traces, med_c, med_p, margin):
    """Split traces into clear class representatives and borderline ones."""
    pools = {c: [] for c in CLASSES}
    border = []
    for t in traces:
        ok_c = t.cache >= med_c * margin if t.cls[:2] == "HC" else t.cache <= med_c / margin
        ok_p = t.pref >= med_p * margin if t.cls[2:] == "HP" else t.pref <= med_p / margin
        (pools[t.cls] if (ok_c and ok_p) else border).append(t)
    return pools, border


def build_plan():
    """The 50 class compositions (see module docstring)."""
    plan = []
    for c in CLASSES:                                   # 8 homogeneous
        plan += [("homogeneous 4", [c] * 4)] * 2
    for a, b in itertools.combinations(CLASSES, 2):     # 12 two-class 2+2
        plan += [("two-class 2+2", [a, a, b, b])] * 2
    for a in CLASSES:                                   # 12 two-class 3+1
        for b in CLASSES:
            if a != b:
                plan.append(("two-class 3+1", [a, a, a, b]))
    for a in CLASSES:                                   # 12 three-class 2+1+1
        for b, c in itertools.combinations([x for x in CLASSES if x != a], 2):
            plan.append(("three-class 2+1+1", [a, a, b, c]))
    plan += [("all four classes", list(CLASSES))] * 6   # 6 all-four
    assert len(plan) == 50
    return plan


def mix_label(classes):
    cnt = Counter(classes)
    return "_".join("%dx%s" % (cnt[c], c) for c in CLASSES if cnt[c])


def generate_mixes(pools, seed):
    rng = random.Random(seed)
    usage, pairs, mixes, seen = Counter(), Counter(), [], set()
    for idx, (mtype, classes) in enumerate(build_plan(), 1):
        for attempt in range(200):                      # retry only if a mix repeats
            chosen = []
            for i in sorted(range(NCORES), key=lambda i: (len(pools[classes[i]]), rng.random())):
                cls = classes[i]
                taken = {t.name for t, _ in chosen}
                fams = {family(t.name) for t, _ in chosen}
                cands = [t for t in pools[cls] if t.name not in taken and family(t.name) not in fams]
                if not cands:                           # pool too small for 4 families
                    cands = [t for t in pools[cls] if t.name not in taken]
                score = lambda t: (usage[t.name] * 10
                                   + 3 * sum(pairs[frozenset((t.name, c.name))] for c, _ in chosen)
                                   + rng.random())
                chosen.append((min(cands, key=score), cls))
            key = frozenset(t.name for t, _ in chosen)
            if key not in seen:
                break
        seen.add(key)
        rng.shuffle(chosen)                             # core placement is randomised too
        for t, _ in chosen:
            usage[t.name] += 1
        for (a, _), (b, _) in itertools.combinations(chosen, 2):
            pairs[frozenset((a.name, b.name))] += 1
        mixes.append(Mix(idx, "mix%02d_%s" % (idx, mix_label(classes)), mtype,
                         tuple(c for _, c in chosen), tuple(t.name for t, _ in chosen)))
    return mixes


def parse_selection(text, n):
    if not text:
        return list(range(1, n + 1))
    sel = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not m:
            sys.exit("[ERROR] bad --only item '%s' (use e.g. 1-5,12,20-22)" % part)
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        sel.update(range(a, b + 1))
    bad = [x for x in sel if not 1 <= x <= n]
    if bad:
        sys.exit("[ERROR] --only: mix ids out of range 1..%d: %s" % (n, sorted(bad)))
    return sorted(sel)


def print_mix_table(mixes, tinfo, borderline, pools, margin, med_c, med_p):
    print("Mix generation: %d clear traces used (margin x%.2f around medians cache=%.4f, prefetch=%.4f)"
          % (sum(len(v) for v in pools.values()), margin, med_c, med_p))
    print("  pool sizes: " + ", ".join("%s=%d" % (c, len(pools[c])) for c in CLASSES))
    if borderline:
        print("  borderline traces not used (%d): %s" % (len(borderline), ", ".join(t.name for t in borderline)))
    print()
    print("%-3s %-34s %-18s %s" % ("id", "mix name", "type", "core0 | core1 | core2 | core3"))
    for m in mixes:
        print("%-3d %-34s %-18s %s" % (m.id, m.name, m.mtype,
              " | ".join("%s[%s]" % (t, c) for t, c in zip(m.traces, m.classes))))
    use = Counter(t for m in mixes for t in m.traces)
    print("\nTrace usage over %d mixes: %d distinct traces, each used %d-%d times (average %.1f)"
          % (len(mixes), len(use), min(use.values()), max(use.values()), sum(use.values()) / len(use)))
    nhc = Counter((sum(c.startswith("HC") for c in m.classes), sum(c.endswith("HP") for c in m.classes))
                  for m in mixes)
    print("(#high-cache cores, #high-prefetch cores) combinations covered by the mixes: %d of 25" % len(nhc))


def write_mixes_csv(path, mixes, trace_dir, tinfo):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        hdr = ["mix_id", "mix_name", "type", "n_high_cache", "n_high_prefetch"]
        for c in range(NCORES):
            hdr += ["core%d_trace" % c, "core%d_class" % c]
        w.writerow(hdr)
        for m in mixes:
            row = [m.id, m.name, m.mtype, sum(c.startswith("HC") for c in m.classes),
                   sum(c.endswith("HP") for c in m.classes)]
            for t, c in zip(m.traces, m.classes):
                row += [t, c]
            w.writerow(row)


# --------------------------------------------------------------------------
# Log parsing / geometry verification
# --------------------------------------------------------------------------
KNOB_RE = {k: re.compile(r"^%s\s+(\d+)\s*$" % k, re.M)
           for k in ("num_cpus", "llc_set", "llc_way", "llc_size", "warmup_instructions",
                     "simulation_instructions")}


def parse_config(text):
    out = {}
    for k, rx in KNOB_RE.items():
        m = rx.search(text)
        out[k] = int(m.group(1)) if m else None
    return out


HEADER_END_RE = re.compile(r"^(?:Heartbeat|Warmup complete|Finished)", re.M)
SKIP_KEY_PREFIX = ("scooby_", "le_", "trace_", "measure_")


def header_config(text):
    """All 'key value' simulator-setup lines printed before the run starts (core, TLB, caches, DRAM, ...),
    without prefetcher-specific and measurement-only knobs, so that e.g. UCP-only and UCP+Pythia compare equal."""
    head = HEADER_END_RE.split(text, maxsplit=1)[0]
    cfg = {}
    for m in re.finditer(r"^([a-z][a-z0-9_]*) (\S+)[ \t]*$", head, re.M):
        k, v = m.group(1), m.group(2)
        if k.startswith(SKIP_KEY_PREFIX) or "prefetcher" in k:
            continue
        cfg[k] = v
    return cfg


def setup_hash(cfg):
    """One fingerprint per experiment: champsim_seed is excluded because it is derived from the traces of each
    mix (it differs between mixes, but is identical for the same mix in different experiments)."""
    items = sorted((k, v) for k, v in cfg.items() if k != "champsim_seed")
    return hashlib.sha1("\n".join("%s=%s" % kv for kv in items).encode()).hexdigest()[:10]


def parse_log(path):
    text = Path(path).read_text(errors="replace")
    res = {"config": parse_config(text), "cores": {}, "setup": header_config(text)}
    for c in range(NCORES):
        def grab(rx, cast=float):
            m = re.search(rx % c, text, re.M)
            return cast(m.group(1)) if m else None
        res["cores"][c] = dict(
            ipc=grab(r"^Core_%d_IPC\s+([0-9.eE+-]+)"),
            ins=grab(r"^Core_%d_instructions\s+(\d+)", int),
            cyc=grab(r"^Core_%d_cycles\s+(\d+)", int),
            llc_acc=grab(r"^Core_%d_LLC_total_access\s+(\d+)", int),
            llc_miss=grab(r"^Core_%d_LLC_total_miss\s+(\d+)", int),
            llc_load_miss=grab(r"^Core_%d_LLC_load_miss\s+(\d+)", int))
    res["complete"] = all(res["cores"][c]["ipc"] is not None for c in range(NCORES))
    return res


def check_geometry(cfg, args):
    bad = []
    for k, v in (("num_cpus", NCORES), ("llc_set", args.llc_sets), ("llc_way", args.llc_ways)):
        if cfg.get(k) != v:
            bad.append("%s=%s (expected %s)" % (k, cfg.get(k), v))
    return bad


def check_config(cfg, args, warm, sim):
    """Return a list of problems (empty = geometry and lengths are as required)."""
    want = {"num_cpus": NCORES, "llc_set": args.llc_sets, "llc_way": args.llc_ways,
            "warmup_instructions": warm, "simulation_instructions": sim}
    bad = []
    for k, v in want.items():
        if cfg.get(k) != v:
            bad.append("%s=%s (expected %s)" % (k, cfg.get(k), v))
    return bad


# --------------------------------------------------------------------------
# Building the 2048-set x 16-way binary (in a throw-away copy of the sources)
# --------------------------------------------------------------------------
def binary_name(args):
    return "%s-multi-pythia-no-%s-%dcore-llc%ds%dw" % (args.bp, args.repl, NCORES, args.llc_sets, args.llc_ways)


SRC_ITEMS = ("Makefile", "build_champsim.sh", "src", "inc", "branch", "prefetcher", "replacement", "config")


def git_list(root, *flags):
    try:
        out = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"] + list(flags) + ["--"] + list(SRC_ITEMS),
                                      stderr=subprocess.DEVNULL)
        return [x for x in out.decode().split("\0") if x]
    except Exception:
        return None


def copy_sources(root, tmp, args):
    """Copy the sources into the temporary build tree.  The Makefile compiles EVERY .cc it finds in
    src/branch/replacement/prefetcher, so stray untracked files left over from other branches (e.g. an old
    prefetcher/l1i_prefetcher.cc) would break the build.  Therefore, in a git checkout only tracked files
    (plus the selected replacement policy / branch predictor) are copied, like a clean clone would have."""
    tracked = None if args.include_untracked else git_list(root)
    if tracked:
        keep = set(tracked) | {"replacement/%s.llc_repl" % args.repl, "branch/%s.bpred" % args.bp}
        stray = [f for f in (git_list(root, "--others", "--exclude-standard") or [])
                 if f not in keep and re.search(r"\.(cc|cpp|h|[a-z0-9]+_pref|llc_repl|bpred)$", f)]
        for rel in sorted(keep):
            src = root / rel
            if src.is_file():
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tmp / rel)
        if stray:
            print("[BUILD] ignoring %d untracked source file(s) (use --include-untracked to keep): %s"
                  % (len(stray), ", ".join(stray[:6]) + (" ..." if len(stray) > 6 else "")))
    else:
        if not args.include_untracked:
            print("[BUILD] note: not a git checkout (or git unavailable) -> copying all sources")
        for item in SRC_ITEMS:
            src, dst = root / item, tmp / item
            shutil.copytree(src, dst) if src.is_dir() else shutil.copy2(src, dst)


def build_binary(root, args):
    if not (root / "libbf/build/lib/libbf.a").is_file():
        sys.exit("[ERROR] libbf is not built. Run once in %s:\n"
                 "  git clone https://github.com/mavam/libbf.git libbf\n"
                 "  cmake -S libbf -B libbf/build && cmake --build libbf/build -j" % root)
    for need in SRC_ITEMS:
        if not (root / need).exists():
            sys.exit("[ERROR] %s/%s not found -- is --root the ChampSim repository?" % (root, need))
    if not (root / "replacement" / ("%s.llc_repl" % args.repl)).is_file():
        sys.exit("[ERROR] replacement policy '%s' not found in %s/replacement" % (args.repl, root))
    if not (root / "branch" / ("%s.bpred" % args.bp)).is_file():
        sys.exit("[ERROR] branch predictor '%s' not found in %s/branch" % (args.bp, root))

    tmp = Path(tempfile.mkdtemp(prefix="champsim_llc%ds%dw_" % (args.llc_sets, args.llc_ways)))
    print("[BUILD] building in a temporary copy: %s (your repository is not modified)" % tmp)
    try:
        copy_sources(root, tmp, args)
        os.symlink(root / "libbf", tmp / "libbf")
        # --- fix the LLC geometry (only in the copy)
        cache_h = tmp / "inc/cache.h"
        txt = cache_h.read_text()
        txt, n1 = re.subn(r"^#define\s+LLC_SET\b.*$", "#define LLC_SET %d" % args.llc_sets, txt, flags=re.M)
        txt, n2 = re.subn(r"^#define\s+LLC_WAY\b.*$", "#define LLC_WAY %d" % args.llc_ways, txt, flags=re.M)
        if n1 != 1 or n2 != 1:
            sys.exit("[ERROR] could not find single '#define LLC_SET/LLC_WAY' lines in inc/cache.h")
        cache_h.write_text(txt)
        # --- UCP derives its sampled-set range from NUM_CPUS*2048; it must follow LLC_SET
        repl_f = tmp / "replacement" / ("%s.llc_repl" % args.repl)
        rtxt = repl_f.read_text()
        rtxt, n3 = re.subn(r"(constexpr\s+uint32_t\s+UCP_NUM_SETS\s*=\s*)[^;]+;",
                           r"\1(uint32_t)(LLC_SET);", rtxt)
        if n3:
            repl_f.write_text(rtxt)
            print("[BUILD] patched UCP_NUM_SETS to follow LLC_SET (%d sets)" % args.llc_sets)
        env = dict(os.environ, PYTHIA_LLC_REPLACEMENT=args.repl, PYTHIA_BRANCH_PRED=args.bp,
                   TERM=os.environ.get("TERM", "xterm"), MAKEFLAGS="-j%d" % args.build_jobs)
        log = root / RESULTS_ROOT / "_build" / ("build_%s.log" % binary_name(args))
        log.parent.mkdir(parents=True, exist_ok=True)
        print("[BUILD] %s -> compiling (log: %s) ..." % (binary_name(args), log))
        with open(log, "w") as lf:
            rc = subprocess.call(["./build_champsim.sh", "multi", "pythia", "no", str(NCORES)],
                                 cwd=str(tmp), env=env, stdout=lf, stderr=subprocess.STDOUT)
        built = tmp / "bin" / ("%s-multi-pythia-no-%s-%dcore" % (args.bp, args.repl, NCORES))
        if rc != 0 or not built.is_file():
            print("".join(log.read_text().splitlines(True)[-25:]))
            sys.exit("[ERROR] build failed (see %s)" % log)
        dest = root / "bin" / binary_name(args)
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        print("[BUILD] done: %s" % dest)
        return dest
    finally:
        if not args.keep_build_dir:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def resolve_binary(root, name):
    p = Path(name)
    cands = [p] if p.is_absolute() else [root / p, root / "bin" / p, Path.cwd() / p]
    for c in cands:
        if c.is_file():
            return c.resolve()
    return cands[0]


def mode_args(root, args):
    if args.mode == "pythia":
        return ["--l2c_prefetcher_types=scooby", "--config=%s" % (root / "config/pythia.ini")]
    if args.mode == "none":
        return ["--config=%s" % (root / "config/nopref.ini")]
    return []   # custom: everything comes from --extra-opts


def build_cmd(binary, root, args, traces, warm, sim):
    return ([str(binary), "--warmup_instructions=%d" % warm, "--simulation_instructions=%d" % sim]
            + mode_args(root, args) + args.extra_opts + ["-traces"] + traces)


def probe_binary(binary, root, args, traces):
    """Start the binary on tiny runs just long enough to read its configuration."""
    cmd = build_cmd(binary, root, args, traces, 1000, 1000)
    p = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True)
    buf, deadline = "", time.time() + 120
    try:
        while time.time() < deadline:
            r, _, _ = select.select([p.stdout], [], [], 1.0)
            if r:
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    break
                buf += chunk.decode(errors="replace")
                cfg = parse_config(buf)
                if cfg["llc_way"] is not None and cfg["num_cpus"] is not None and cfg["llc_set"] is not None:
                    return cfg
            elif p.poll() is not None:
                break
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            pass
        p.wait()
    return parse_config(buf)


class Runner:
    def __init__(self, binary, root, args, warm, sim, trace_dir, results):
        self.binary, self.root, self.args = binary, root, args
        self.warm, self.sim, self.trace_dir, self.results = warm, sim, trace_dir, Path(results)
        self.lock, self.procs, self.stop = threading.Lock(), {}, threading.Event()

    def mix_dir(self, m):
        return self.results / m.name

    def final_log(self, m):
        return self.mix_dir(m) / ("%s.txt" % m.name)

    def is_done(self, m):
        f = self.final_log(m)
        return f.is_file() and parse_log(f)["complete"]

    def say(self, msg):
        with self.lock:
            print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)

    def run_mix(self, m):
        if self.stop.is_set():
            return m, "CANCELLED", 0.0
        traces = [str(self.trace_dir / ("%s.champsimtrace.xz" % t)) for t in m.traces]
        cmd = build_cmd(self.binary, self.root, self.args, traces, self.warm, self.sim)
        mdir = self.mix_dir(m)
        mdir.mkdir(parents=True, exist_ok=True)
        for old in list(mdir.glob("%s.*.txt" % m.name)) + [self.final_log(m)]:   # stale logs of earlier tries
            if old.exists():
                old.unlink()
        (mdir / "command.txt").write_text(" ".join(cmd) + "\n")
        tmp_log, final = mdir / "run.running", self.final_log(m)
        self.say("[START] %s" % m.name)
        t0 = time.time()
        with open(tmp_log, "w") as lf:
            p = subprocess.Popen(cmd, cwd=str(self.root), stdout=lf, stderr=subprocess.STDOUT,
                                 start_new_session=True)
            with self.lock:
                self.procs[m.id] = p
            status = None
            try:
                limit = self.args.timeout_hours * 3600 if self.args.timeout_hours else None
                p.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                status = "TIMEOUT"
            finally:
                with self.lock:
                    self.procs.pop(m.id, None)
        wall = time.time() - t0
        if status is None:
            if self.stop.is_set():
                status = "CANCELLED"
            elif p.returncode != 0:
                status = "FAILED(rc=%d)" % p.returncode
            elif not parse_log(tmp_log)["complete"]:
                status = "FAILED(incomplete log)"
            else:
                status = "OK"
        if status == "OK":
            os.replace(tmp_log, final)
        elif status == "CANCELLED":
            tmp_log.unlink()
            (mdir / "command.txt").unlink()
            try:
                mdir.rmdir()
            except OSError:
                pass
        else:
            os.replace(tmp_log, mdir / ("%s.%s.txt" % (m.name, re.sub(r"\W+", "_", status).strip("_"))))
        if status != "CANCELLED":
            (mdir / "status.txt").write_text("%s  %.1f min  %s\n" % (status, wall / 60, datetime.now().isoformat(timespec="seconds")))
        self.say("[%s] %s (%.1f min)" % (status, m.name, wall / 60))
        return m, status, wall

    def kill_all(self):
        self.stop.set()
        with self.lock:
            for p in self.procs.values():
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except Exception:
                    pass


def write_summary(results, mixes, args, warm, sim, walls):
    rows = []
    for m in mixes:
        f = Path(results) / m.name / ("%s.txt" % m.name)
        row = {"mix_id": m.id, "mix_name": m.name, "type": m.mtype}
        if f.is_file():
            pl = parse_log(f)
            bad = check_config(pl["config"], args, warm, sim)
            row["status"] = "OK" if pl["complete"] else "INCOMPLETE"
            row["config_ok"] = "yes" if not bad else "NO: " + "; ".join(bad)
            row["llc_sets"], row["llc_ways"] = pl["config"]["llc_set"], pl["config"]["llc_way"]
            row["setup_hash"] = setup_hash(pl["setup"])
            ipcs = [pl["cores"][c]["ipc"] for c in range(NCORES)]
            miss = [pl["cores"][c]["llc_miss"] for c in range(NCORES)]
            lmiss = [pl["cores"][c]["llc_load_miss"] for c in range(NCORES)]
            for c in range(NCORES):
                row["core%d_trace" % c] = m.traces[c]
                row["core%d_ipc" % c] = ipcs[c]
            row["sum_ipc"] = round(sum(x for x in ipcs if x is not None), 6) if pl["complete"] else ""
            for c in range(NCORES):
                row["core%d_llc_misses" % c] = miss[c]
            for c in range(NCORES):
                row["core%d_llc_load_misses" % c] = lmiss[c]
            row["llc_misses_total"] = sum(x for x in miss if x is not None) if pl["complete"] else ""
            row["wall_min"] = round(walls[m.id] / 60, 1) if m.id in walls else ""
        else:
            row["status"] = "NOT RUN"
        rows.append(row)
    cols = (["mix_id", "mix_name", "type", "status", "config_ok", "llc_sets", "llc_ways", "setup_hash"]
            + sum([["core%d_trace" % c, "core%d_ipc" % c] for c in range(NCORES)], [])
            + ["sum_ipc"] + ["core%d_llc_misses" % c for c in range(NCORES)]
            + ["core%d_llc_load_misses" % c for c in range(NCORES)] + ["llc_misses_total", "wall_min"])
    path = Path(results) / "summary.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return path, rows


def compare_setups(dirs):
    """Check that experiments were run with the same simulator setup and the same mixes."""
    per = {}
    for d in dirs:
        logs = {}
        for f in sorted(Path(d).glob("mix*/mix*.txt")):
            pl = parse_log(f)
            if pl["complete"]:
                traces = ",".join(os.path.basename(x) for x in
                                  re.findall(r"^trace_\d+ (\S+)", f.read_text(errors="replace"), re.M))
                logs[f.parent.name] = (pl["setup"], traces)
        per[str(d)] = logs
        hs = Counter(setup_hash(v[0]) for v in logs.values())
        print("%-62s %2d complete logs, setup fingerprint(s): %s" % (d, len(logs), ", ".join(hs) or "-"))
    ref_name = str(dirs[0])
    ref, problems = per[ref_name], 0
    for d in dirs[1:]:
        cur = per[str(d)]
        common = sorted(set(ref) & set(cur))
        bad_setup, bad_mix = [], []
        for m in common:
            diff = {k: (ref[m][0].get(k), cur[m][0].get(k)) for k in set(ref[m][0]) | set(cur[m][0])
                    if ref[m][0].get(k) != cur[m][0].get(k)}
            if diff:
                bad_setup.append((m, diff))
            if ref[m][1] != cur[m][1]:
                bad_mix.append(m)
        print("\n%s  vs  %s" % (d, ref_name))
        print("  mixes compared: %d | same simulator setup: %d | same traces: %d" % (
            len(common), len(common) - len(bad_setup), len(common) - len(bad_mix)))
        if set(ref) ^ set(cur):
            print("  mixes present in only one of the two: %d" % len(set(ref) ^ set(cur)))
        for m, diff in bad_setup[:1]:
            print("  DIFFERENT SETUP (e.g. %s): %s" % (m, "; ".join("%s: %s vs %s" % (k, a, b) for k, (a, b) in sorted(diff.items())[:6])))
        if bad_mix:
            print("  DIFFERENT TRACES in: %s" % ", ".join(bad_mix[:5]))
        if not common:
            print("  nothing to compare (no mix has a complete log in both folders)")
        problems += len(bad_setup) + len(bad_mix) + (0 if common else 1)
    print("\n" + ("RESULT: identical simulator setup and mixes in all compared experiments." if not problems
                  else "RESULT: MISMATCH found -- do not compare these experiments directly."))
    return 1 if problems else 0


# --------------------------------------------------------------------------
def main():
    here = Path(__file__).resolve().parent
    default_root = here.parent if here.name == "scripts" else here
    ap = argparse.ArgumentParser(
        description="Run 50 four-core mixes (class-balanced by cache/prefetch sensitivity) in parallel.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = ap.add_argument_group("simulation")
    g.add_argument("--warmup", type=float, default=WARMUP_M, help="warm-up instructions per core, in millions")
    g.add_argument("--sim", type=float, default=SIM_M, help="simulated instructions per core, in millions")
    g.add_argument("--llc-sets", type=int, default=LLC_SETS, help="required LLC sets (shared by the 4 cores)")
    g.add_argument("--llc-ways", type=int, default=LLC_WAYS, help="required LLC ways")
    g.add_argument("-j", "--jobs", type=int, default=int(os.environ.get("PARALLEL_JOBS", PARALLEL_JOBS)),
                   help="number of simulations running at the same time (default: env PARALLEL_JOBS, else the "
                        "PARALLEL_JOBS value at the top of this file)")
    g.add_argument("--results-dir", type=Path, default=None,
                   help="results folder of this experiment (default: <root>/<RESULTS_ROOT>/<label>)")
    g.add_argument("--label", default=None, help="experiment name = results sub-folder (default: <repl> for "
                   "--mode none, <repl>_<mode> otherwise, e.g. ucp, ucp_pythia)")
    g.add_argument("--compare-setups", nargs="+", type=Path, metavar="DIR",
                   help="compare the simulator setup + mixes of two or more results folders and exit")
    g.add_argument("--only", default="", help="run only these mix ids, e.g. 1-5,12,20-22")
    g.add_argument("--force", action="store_true", help="re-run mixes that already have a complete log")
    g.add_argument("--timeout-hours", type=float, default=0, help="kill a mix after this long (0 = no limit)")
    g.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    g.add_argument("--list", action="store_true", help="print the 50 mixes and exit")
    g.add_argument("--summarize-only", action="store_true", help="rebuild summary.csv from existing logs and exit")
    b = ap.add_argument_group("binary")
    b.add_argument("--binary", default=None, help="ChampSim binary (name in bin/ or a path); default is derived "
                   "from --bp/--repl, e.g. bin/perceptron-multi-pythia-no-ucp-4core-llc2048s16w")
    b.add_argument("--repl", default="ucp", help="LLC replacement policy (replacement/<name>.llc_repl)")
    b.add_argument("--bp", default="perceptron", help="branch predictor (branch/<name>.bpred)")
    b.add_argument("--build", action="store_true", help="(re)build the binary with the fixed LLC geometry first")
    b.add_argument("--build-only", action="store_true", help="build and exit")
    b.add_argument("--build-jobs", type=int, default=os.cpu_count() or 4, help="make -j for --build")
    b.add_argument("--include-untracked", action="store_true",
                   help="--build: also copy untracked source files (default: tracked files only, like a clean clone)")
    b.add_argument("--keep-build-dir", action="store_true", help="keep the temporary build directory")
    b.add_argument("--mode", choices=["pythia", "none", "custom"], default="pythia",
                   help="pythia: --l2c_prefetcher_types=scooby + config/pythia.ini; none: config/nopref.ini; "
                        "custom: only --extra-opts")
    b.add_argument("--extra-opts", default="", help="extra simulator options, e.g. '--config=/path/x.ini'")
    b.add_argument("--no-verify", action="store_true", help="skip the LLC-geometry check (not recommended)")
    m = ap.add_argument_group("mixes")
    m.add_argument("--seed", type=int, default=1, help="seed of the mix generator (same seed = same 50 mixes)")
    m.add_argument("--margin", type=float, default=1.75,
                   help="only use traces at least this factor away from the High/Low median on both axes (1 = use all)")
    m.add_argument("--classes-xlsx", type=Path, default=None, help="read classes from a workbook instead of the "
                   "built-in copy of the 'Tacker Segregation' tab")
    m.add_argument("--write-mixes", type=Path, default=None, help="write the 50 mixes to this CSV file (with --list "
                   "nothing else is done)")
    m.add_argument("--trace-dir", type=Path, default=None, help="directory with *.champsimtrace.xz (default <root>/dpc3_traces)")
    ap.add_argument("--root", type=Path, default=default_root, help="ChampSim repository root")
    args = ap.parse_args()
    if args.compare_setups:
        if len(args.compare_setups) < 2:
            sys.exit("[ERROR] --compare-setups needs at least two results folders")
        sys.exit(compare_setups(args.compare_setups))
    args.extra_opts = args.extra_opts.split()
    root = args.root.resolve()
    trace_dir = (args.trace_dir or root / "dpc3_traces").resolve()
    warm, sim = int(round(args.warmup * 1_000_000)), int(round(args.sim * 1_000_000))
    if args.jobs < 1:
        sys.exit("[ERROR] --jobs must be >= 1")
    if args.margin < 1:
        sys.exit("[ERROR] --margin must be >= 1")

    # ---- mixes
    if args.classes_xlsx:
        traces, med_c, med_p = load_traces_from_xlsx(args.classes_xlsx)
    else:
        traces, med_c, med_p = embedded_traces(), MEDIAN_CACHE, MEDIAN_PREFETCH
    pools, border = clear_pools(traces, med_c, med_p, args.margin)
    for c in CLASSES:
        if len({family(t.name) for t in pools[c]}) < NCORES:
            sys.exit("[ERROR] class %s has fewer than 4 distinct benchmark families; lower --margin" % c)
    mixes = generate_mixes(pools, args.seed)
    if args.write_mixes:
        write_mixes_csv(args.write_mixes, mixes, trace_dir, traces)
        print("mixes written: %s" % args.write_mixes)
    if args.list:
        print_mix_table(mixes, traces, border, pools, args.margin, med_c, med_p)
        return
    if args.write_mixes and not (args.build or args.build_only or args.summarize_only):
        return
    selected = [mixes[i - 1] for i in parse_selection(args.only, len(mixes))]

    # ---- binary
    binary = resolve_binary(root, args.binary or str(Path("bin") / binary_name(args)))
    if args.build or args.build_only:
        if args.binary:
            sys.exit("[ERROR] --build creates its own binary name; do not combine with --binary")
        binary = build_binary(root, args)
        if args.build_only:
            return
    label = args.label or (args.repl if args.mode == "none" else "%s_%s" % (args.repl, args.mode))
    results = (args.results_dir or root / RESULTS_ROOT / label).resolve()
    results.mkdir(parents=True, exist_ok=True)

    if args.summarize_only:
        path, rows = write_summary(results, mixes, args, warm, sim, {})
        print("summary written: %s  (%d of %d mixes have logs)" % (path, sum(r["status"] != "NOT RUN" for r in rows), len(rows)))
        return

    if not binary.is_file() or not os.access(binary, os.X_OK):
        sys.exit("[ERROR] binary not found: %s\n  Build it with:  python3 %s --build --build-only"
                 % (binary, Path(__file__).name))

    # ---- traces
    need = sorted({t for m in selected for t in m.traces})
    missing = [t for t in need if not (trace_dir / ("%s.champsimtrace.xz" % t)).is_file()]
    if missing:
        sys.exit("[ERROR] %d trace file(s) missing in %s:\n  %s" % (len(missing), trace_dir, "\n  ".join(missing)))

    write_mixes_csv(results / "mixes.csv", mixes, trace_dir, traces)
    runner = Runner(binary, root, args, warm, sim, trace_dir, results)

    # ---- verify LLC geometry of the binary BEFORE launching hours of simulation
    if not args.no_verify and not args.dry_run:
        probe_traces = [str(trace_dir / ("%s.champsimtrace.xz" % t)) for t in selected[0].traces]
        cfg = probe_binary(binary, root, args, probe_traces)
        bad = check_geometry(cfg, args)
        if cfg.get("llc_set") is None:
            sys.exit("[ERROR] could not read the LLC configuration from %s (is it a Pythia-based ChampSim "
                     "binary?). Use --no-verify to skip this check." % binary.name)
        print("[CHECK] %s: cores=%s  LLC=%s sets x %s ways (%s KB)  -> %s" % (
            binary.name, cfg["num_cpus"], cfg["llc_set"], cfg["llc_way"], cfg["llc_size"],
            "OK" if not bad else "MISMATCH"))
        if bad:
            sys.exit("[ERROR] this binary does not have the required geometry: %s\n"
                     "  Build the right one with:  python3 %s --build --build-only --repl %s"
                     % ("; ".join(bad), Path(__file__).name, args.repl))

    todo = [m for m in selected if args.force or not runner.is_done(m)]
    skipped = len(selected) - len(todo)
    with open(results / "run_manifest.txt", "a") as mf:
        mf.write("==== %s on %s\ncommand : %s\nlabel   : %s\nbinary  : %s\nmode    : %s %s\nwarmup  : %g M, sim: %g M per core\n"
                 "LLC     : %d sets x %d ways\njobs    : %d\nmix seed: %d, margin %.2f\nmixes   : %s\n\n" % (
                     datetime.now().isoformat(timespec="seconds"), socket.gethostname(), " ".join(sys.argv),
                     label, binary, args.mode, " ".join(args.extra_opts), args.warmup, args.sim, args.llc_sets,
                     args.llc_ways, args.jobs, args.seed, args.margin, ",".join(str(m.id) for m in selected)))
    print("Experiment: %s\nBinary : %s\nResults: %s\nMixes  : %d selected, %d already done, %d to run | jobs=%d | warm-up %gM + sim %gM per core | LLC %dx%d"
          % (label, binary, results, len(selected), skipped, len(todo), args.jobs, args.warmup, args.sim, args.llc_sets, args.llc_ways))

    if args.dry_run:
        for m in todo:
            traces_ = [str(trace_dir / ("%s.champsimtrace.xz" % t)) for t in m.traces]
            print("\n# %s\n%s > %s" % (m.name, " ".join(build_cmd(binary, root, args, traces_, warm, sim)),
                                       results / ("%s.txt" % m.name)))
        return

    walls, outcome = {}, Counter()

    def _stop(*_):
        raise KeyboardInterrupt
    # explicit handlers: also works when the shell started us in the background (where SIGINT is ignored)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    t_start = time.time()
    pool = ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futs = [pool.submit(runner.run_mix, m) for m in todo]
        for fu in as_completed(futs):
            m, status, wall = fu.result()
            walls[m.id] = wall
            outcome[status.split("(")[0]] += 1
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] stopping running simulations ...", flush=True)
        runner.kill_all()          # queued mixes see the stop flag and return immediately
        pool.shutdown(wait=True)
        write_summary(results, mixes, args, warm, sim, walls)
        sys.exit(130)
    pool.shutdown(wait=True)

    path, rows = write_summary(results, mixes, args, warm, sim, walls)
    print("\nFinished in %.1f min: %s" % ((time.time() - t_start) / 60, dict(outcome) or "nothing to run"))
    bad_cfg = [r["mix_name"] for r in rows if r["status"] == "OK" and r["config_ok"] != "yes"]
    if bad_cfg:
        print("[WARN] LLC/length configuration differs from the request in: %s" % ", ".join(bad_cfg))
    hashes = Counter(r["setup_hash"] for r in rows if r["status"] == "OK" and r.get("setup_hash"))
    if len(hashes) == 1:
        r0 = next(r for r in rows if r.get("setup_hash"))
        print("Simulator setup fingerprint: %s (identical in all %d logs; LLC in the logs: %s sets x %s ways)" % (
            next(iter(hashes)), sum(hashes.values()), r0["llc_sets"], r0["llc_ways"]))
    elif hashes:
        print("[WARN] logs in this folder come from DIFFERENT simulator setups: %s" % dict(hashes))
    print("Summary: %s" % path)
    if any(k != "OK" for k in outcome):
        sys.exit(1)


if __name__ == "__main__":
    main()
