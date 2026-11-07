#!/usr/bin/env python3
"""
run_4core_mixes_ipcp.py  --  50 four-core workload mixes, UCP / IPCP experiments, run in parallel
===============================================================================
Same 50 mixes, same warm-up / simulation length, same LLC (2048 sets x 16 ways = 2 MB shared by the 4 cores),
same results layout and the same multi-threaded runner as run_4core_mixes.py (the UCP / UCP_PYTHIA / PYTHIA version),
but for the DPC-3 / IPCP ChampSim tree (UCP + IPCP): it drives  ./build_champsim.sh  and the
"-warmup_instructions N -simulation_instructions N -traces ..." command line of that simulator.

The five experiments  (--experiment NAME,  or  --experiment all  for all five in one command)
    name             L1D pref.  L2C pref.   LLC policy                 results folder
    ucp              -          -           UCP partitioning           results_4core_mixes_ipcp/ucp
    ucp_ipcp_l2      -          IPCP        UCP partitioning           results_4core_mixes_ipcp/ucp_ipcp_l2
    ucp_ipcp_l1_l2   IPCP       IPCP        UCP partitioning           results_4core_mixes_ipcp/ucp_ipcp_l1_l2
    ipcp_l2          -          IPCP        plain (SRRIP, --repl lru)  results_4core_mixes_ipcp/ipcp_l2_srrip
    ipcp_l1_l2       IPCP       IPCP        plain (SRRIP, --repl lru)  results_4core_mixes_ipcp/ipcp_l1_l2_srrip
  extra (not part of 'all'):  ucp_ipcp_l1 (IPCP at L1D only + UCP)   ipcp_l1 (IPCP at L1D only, plain LLC)

  IMPORTANT about the "L2 only" experiments: the L2 part of IPCP does not have its own classifier.  It is driven by the
  class / stride that the L1D part sends along with every prefetch (metadata).  With no L1D prefetcher that metadata is
  always 0, so L2-only IPCP can only issue its "new IP -> next line" prefetch.  ucp_ipcp_l2 / ipcp_l2 are therefore
  close to their no-prefetcher baselines -- that is what the code does, not a bug of the script.  If you meant
  "IPCP at the L1D only", use the extra experiments ucp_ipcp_l1 / ipcp_l1.

KNOB EXPERIMENTS -- fixed LLC ways and a fixed IPCP degree (for the coordination study)
  Two knobs can be set per experiment; they use the experiment hooks that your branch already has, so NOTHING is patched:
      +ways=A-B-C-D       the UCP way allocation is PINNED at this split (A+B+C+D = LLC ways, each >= 1), UMON / repartitioning
                          off  (environment variable UCP_FIXED_ALLOCATION of replacement/ucp.llc_repl);  +ways=equal = 4-4-4-4
      +deg=N              upper bound of the IPCP prefetch degree of every core;  +deg=N0-N1-N2-N3 = one bound per core
                          (environment variable IPCP_DEGREE_CAPS of the IPCP L1D / L2C prefetchers).  IPCP's own degrees are GS 6,
                          CS 3, CPLX 3, NL 1, so +deg=1 is the least aggressive setting; 0 switches IPCP off for that core.
  Presets (the first seven are the original ones)
      ucp ucp_ipcp_l2 ucp_ipcp_l1_l2 ipcp_l2 ipcp_l1_l2 ucp_ipcp_l1 ipcp_l1     (see the table above)
      plain               plain LLC (SRRIP, --repl lru for LRU), NO prefetcher                    -> results_4core_mixes_ipcp/srrip
      eq                  UCP's way-owned LLC pinned at the equal split 4/4/4/4, no prefetcher     -> .../eq
      eq_ipcp_l1_l2       the same equal split + IPCP at L1D and L2C                               -> .../eq_ipcp_l1_l2
      eq_ipcp_l2, eq_ipcp_l1   equal split + IPCP at the L2C only / the L1D only
  Groups (comma separated lists are fine:  --experiment ucp,eq_ipcp_l1_l2+deg=2)
      all          ucp, ucp_ipcp_l2, ucp_ipcp_l1_l2, ipcp_l2, ipcp_l1_l2                           (as before)
      grid         ucp, ucp_ipcp_l1_l2, ipcp_l1_l2, plain, eq, eq_ipcp_l1_l2   = {plain LLC, UCP, equal split} x {no prefetcher, IPCP}
      degrees      {ucp_ipcp_l1_l2, eq_ipcp_l1_l2, ipcp_l1_l2} x --degrees (default 1,2,4)  -> e.g. .../ucp_ipcp_l1_l2_d2
      everything   all + grid + degrees
  Result folders get a suffix for the knobs:  _w2-2-6-6 (ways)  _d2 (same degree everywhere)  _d0-4-4-0 (per core).
  Examples
      python3 run_4core_mixes_ipcp.py --experiment grid --build --jobs 32
      python3 run_4core_mixes_ipcp.py --experiment degrees --build --jobs 32
      python3 run_4core_mixes_ipcp.py --experiment ucp_ipcp_l1_l2+deg=0-4-4-0,ucp+ways=2-2-6-6 --build --jobs 32
      python3 run_4core_mixes_ipcp.py --list-experiments                      # presets, groups, knob syntax
  The knobs are environment variables of the simulation processes only: the binaries are the same ones as without knobs
  (an experiment without +ways / +deg / eq runs exactly the original logic), your repository is never modified and there is
  nothing to restore.  --build stops with a clear message if the tree does not contain the hooks (older branches).  The knobs
  are recorded in the first line of every log, in command.txt and in run_manifest.txt (ways / degree / env).

What it does
  1. Builds the 50 four-core mixes from the 64 single-core traces classified in the workbook
     "IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx" (tab "Tacker Segregation"; a copy of the table is built in) -- the
     SAME generator and seed as run_4core_mixes.py, so the mixes are identical to the UCP / UCP_PYTHIA / PYTHIA runs.
         HCHP = High-cache / High-prefetch     HCLP = High-cache / Low-prefetch
         LCHP = Low-cache  / High-prefetch     LCLP = Low-cache  / Low-prefetch
         type                 mixes   example
         homogeneous            8     4xHCHP            (2 mixes per class)
         two-class 2+2         12     2xHCHP + 2xLCLP   (6 class pairs x 2)
         two-class 3+1         12     3xHCLP + 1xLCHP   (12 ordered class pairs)
         three-class 2+1+1     12     2xHCHP + 1xHCLP + 1xLCLP
         all four classes       6     1xHCHP + 1xHCLP + 1xLCHP + 1xLCLP
  2. Runs each mix with 50M warm-up + 200M simulated instructions per core (change with --warmup / --sim, in millions).
  3. --build compiles one binary per (L1D prefetcher, L2C prefetcher, LLC policy) in a THROW-AWAY COPY of the sources:
     your repository is not touched.  In that copy the script
        * fixes the LLC geometry:  LLC_SET = 2048, LLC_WAY = 16 (--llc-sets / --llc-ways)   [repo default is 4096 sets]
        * adds a small READ-ONLY statistics hook (it does not change any simulated result).  The log then ends with the
          same  Core_<n>_<stat>  lines that the Pythia ChampSim prints (instructions, cycles, IPC, L1D/L2C/LLC accesses,
          hits, misses, load misses, average miss latency, prefetch issued / useful / useless, LLC occupancy of every
          core, DRAM row-buffer statistics, UCP final quotas) plus the IPCP per-class counters
          (Core_<n>_L1D_IPCP_GS_filled / _useful / _late, CS, CPLX, NL, and the same for L2C),
          so that make_ucp_vs_ipcp_excel.py can turn every log into the Excel workbook.
     and the script verifies the geometry of whatever binary you run (before starting, and again in every log).
  4. Parallelism: --jobs N (or PARALLEL_JOBS at the top of this file) simulations run at the same time.  With
     --experiment all all 250 simulations (5 experiments x 50 mixes) share that one thread pool, interleaved mix by mix.
  5. Every simulation gets its OWN folder, grouped by experiment:
         results_4core_mixes_ipcp/<experiment>/
             mix01_4xHCHP/mix01_4xHCHP.txt            full simulator output (log)
             mix01_4xHCHP/command.txt, status.txt     exact command line, result and run time
             mixes.csv  summary.csv  run_manifest.txt
     Finished mixes are skipped when you re-run (--force redoes them).

Quick start (from the ChampSim repo root, or anywhere with --root)
    python3 run_4core_mixes_ipcp.py --list                                   # show the 50 mixes
    python3 run_4core_mixes_ipcp.py --experiment all --build --build-only    # build the 5 binaries (LLC 2048 x 16)
    python3 run_4core_mixes_ipcp.py --experiment all --jobs 32               # 5 x 50 mixes, 32 threads
    python3 run_4core_mixes_ipcp.py --experiment ucp_ipcp_l1_l2 --jobs 8     # a single experiment
    python3 run_4core_mixes_ipcp.py --experiment all --repl lru --jobs 32    # IPCP-alone experiments on an LRU LLC
    python3 run_4core_mixes_ipcp.py --experiment all --only 1-3 --warmup 5 --sim 20 --jobs 8   # quick trial
    python3 run_4core_mixes_ipcp.py --experiment all --dry-run               # print the commands only
    python3 run_4core_mixes_ipcp.py --summarize-only --experiment all        # rebuild summary.csv from the logs
After the runs:
    python3 run_4core_mixes_ipcp.py --compare-setups results_4core_mixes_ipcp/*      # same setup + mixes in all folders?
    python3 make_ucp_vs_ipcp_excel.py                                                 # Excel workbook with plots

Only the Python standard library is needed (openpyxl only for --classes-xlsx).
===============================================================================
"""
import argparse
import copy
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
PARALLEL_JOBS = 25                      # number of 4-core simulations running at the same time
#                                        precedence:  --jobs  >  env PARALLEL_JOBS  >  this value
WARMUP_M = 50                          # warm-up instructions per core, in millions
SIM_M = 200                            # simulated instructions per core, in millions
LLC_SETS = 2048                        # required LLC sets  (one LLC shared by the 4 cores) - same as the UCP / PYTHIA runs
LLC_WAYS = 16                          # required LLC ways
RESULTS_ROOT = "results_4core_mixes_ipcp"   # under the repo root; one sub-folder per experiment
# ============================================================================

IPCP = "ipcp_isca2020"
# --experiment presets:  (L1D prefetcher, L2C prefetcher, LLC replacement policy).  A policy of None = "plain LLC, chosen
# with --repl" (default SRRIP, like the Pythia-alone experiment of the UCP_PYTHIA study).  The eq_* presets use UCP's
# way-owned LLC with the way allocation pinned at the equal split.
EXPERIMENTS = {"ucp": ("no", "no", "ucp"),
               "ucp_ipcp_l2": ("no", IPCP, "ucp"),
               "ucp_ipcp_l1_l2": (IPCP, IPCP, "ucp"),
               "ipcp_l2": ("no", IPCP, None),
               "ipcp_l1_l2": (IPCP, IPCP, None),
               "ucp_ipcp_l1": (IPCP, "no", "ucp"),        # extra: IPCP at L1D only
               "ipcp_l1": (IPCP, "no", None),             # extra: IPCP at L1D only, plain LLC
               "plain": ("no", "no", None),               # plain LLC, no prefetcher
               "eq": ("no", "no", "ucp"),
               "eq_ipcp_l2": ("no", IPCP, "ucp"),
               "eq_ipcp_l1_l2": (IPCP, IPCP, "ucp"),
               "eq_ipcp_l1": (IPCP, "no", "ucp")}
EQUAL_PRESETS = {"eq": "ucp", "eq_ipcp_l2": "ucp_ipcp_l2", "eq_ipcp_l1_l2": "ucp_ipcp_l1_l2", "eq_ipcp_l1": "ucp_ipcp_l1"}
PLAIN_DEFAULT_REPL = "srrip"
ALL_EXPERIMENTS = ("ucp", "ucp_ipcp_l2", "ucp_ipcp_l1_l2", "ipcp_l2", "ipcp_l1_l2")   # what  --experiment all  runs
GRID_EXPERIMENTS = ("ucp", "ucp_ipcp_l1_l2", "ipcp_l1_l2", "plain", "eq", "eq_ipcp_l1_l2")
DEGREE_BASES = ("ucp_ipcp_l1_l2", "eq_ipcp_l1_l2", "ipcp_l1_l2")           # what  --experiment degrees  sweeps
DEFAULT_DEGREES = "1,2,4"
GROUPS = ("all", "grid", "degrees", "everything")

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


def parse_vector(text, what, ap, allow_equal=False, minimum=0):
    """'2-2-6-6' (one value per core) | '2' (the same for all cores) | 'equal' -> tuple of ints, or the string 'equal'."""
    t = str(text).strip().lower()
    if allow_equal and t == "equal":
        return "equal"
    try:
        v = [int(x) for x in re.split(r"[-/:]", t)]
    except ValueError:
        ap.error("%s: '%s' is not a number or a list like 2-2-6-6" % (what, text))
    if len(v) == 1:
        v = v * NCORES
    if len(v) != NCORES or any(x < minimum for x in v):
        ap.error("%s: '%s' needs %d values >= %d (or one value for all cores)" % (what, text, NCORES, minimum))
    return tuple(v)


def vec_text(v):
    return str(v[0]) if len(set(v)) == 1 else "-".join(str(x) for x in v)


def ways_str(v):
    """Way splits are always written in full (2-2-6-6, 4-4-4-4), never collapsed."""
    return "-".join(str(x) for x in v)


def equal_split(ways):
    """Equal split of the LLC ways, the remainder going to the first cores (as in the UCP code)."""
    return tuple(ways // NCORES + (1 if i < ways % NCORES else 0) for i in range(NCORES))


def expand_specs(text, args, ap):
    """--experiment text -> list of experiment specs ('name' or 'name+ways=..+deg=..'), groups expanded, no duplicates."""
    degrees = [d for d in re.split(r"[,\s]+", str(args.degrees).strip()) if d]
    if not degrees or not all(d.isdigit() and int(d) >= 1 for d in degrees):
        ap.error("--degrees expects a comma separated list of integers >= 1 (got '%s')" % args.degrees)
    sweep = ["%s+deg=%s" % (b, d) for b in DEGREE_BASES for d in degrees]
    out = []
    for item in [x.strip() for x in text.split(",") if x.strip()]:
        if item == "all":
            out += ALL_EXPERIMENTS
        elif item == "grid":
            out += GRID_EXPERIMENTS
        elif item == "degrees":
            out += sweep
        elif item == "everything":
            out += list(ALL_EXPERIMENTS) + list(GRID_EXPERIMENTS) + sweep
        else:
            out.append(item)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def make_experiment(args, spec, ap, multi):
    """Experiment settings (prefetchers, LLC policy, label, knobs, environment) of one experiment spec.  spec = None is the
    old spelling without --experiment (--l1d-pref / --l2c-pref / --repl / --label)."""
    a = copy.copy(args)
    a.ways = a.deg = None
    a.equal = False
    name, knobs = None, []
    if spec:
        name, *knobs = spec.split("+")
        if name not in EXPERIMENTS:
            ap.error("unknown experiment '%s' (presets: %s; groups: %s)" % (name, ", ".join(EXPERIMENTS), ", ".join(GROUPS)))
    for k in knobs:
        key, _, val = k.partition("=")
        if key == "ways":
            a.ways = parse_vector(val, "+ways", ap, allow_equal=True, minimum=1)
        elif key in ("deg", "degree"):
            a.deg = parse_vector(val, "+deg", ap, minimum=0)
        else:
            ap.error("unknown knob '%s' in '%s' (use +ways=A-B-C-D or +deg=N)" % (k, spec))
    if a.ways == "equal":                       # 'ucp+ways=equal' is the preset 'eq'
        if name not in EQUAL_PRESETS.values():
            ap.error("+ways=equal needs a UCP experiment (ucp / ucp_ipcp_*), got '%s'" % spec)
        name = next(k for k, v in EQUAL_PRESETS.items() if v == name)
        a.ways = None
    if name in EQUAL_PRESETS:
        a.equal = True
        if a.ways is not None:
            ap.error("'%s' already uses the equal split: give either the preset or +ways" % spec)
    a.experiment, a.spec = name, spec
    if name:
        l1d, l2c, p_repl = EXPERIMENTS[name]
        for opt, val, flag in (("l1d_pref", l1d, "--l1d-pref"), ("l2c_pref", l2c, "--l2c-pref")):
            if getattr(args, opt) not in (None, val):
                ap.error("--experiment %s implies %s %s (got %s)" % (name, flag, val, getattr(args, opt)))
        a.l1d_pref, a.l2c_pref = l1d, l2c
        if p_repl is None:                                  # plain LLC policy of the user's choice
            a.repl = args.repl or PLAIN_DEFAULT_REPL
            if a.repl == "ucp":
                ap.error("--experiment %s has NO UCP partitioning: use --repl srrip or lru (UCP experiments: ucp, "
                         "ucp_ipcp_*, eq*)" % name)
        else:
            if args.repl not in (None, p_repl) and not multi:
                ap.error("--experiment %s implies --repl %s (got --repl %s)" % (name, p_repl, args.repl))
            a.repl = p_repl
    else:
        if not (args.l1d_pref and args.l2c_pref and args.repl):
            ap.error("choose --experiment %s | all | grid | degrees | everything   (or give all of --l1d-pref --l2c-pref --repl)"
                     % " | ".join(sorted(EXPERIMENTS)))
    uses_ipcp = IPCP in (a.l1d_pref, a.l2c_pref)
    # ---- global --ways / --degree: applied where they make sense
    if a.ways is None and not a.equal and args.ways_all and a.repl == "ucp":
        a.ways = parse_vector(args.ways_all, "--ways", ap, minimum=1)
    if a.deg is None and args.degree_all is not None and uses_ipcp:
        a.deg = parse_vector(args.degree_all, "--degree", ap, minimum=0)
    if a.ways is not None:
        if a.repl != "ucp":
            ap.error("+ways needs a UCP experiment (the LLC policy of '%s' is %s)" % (spec or "this run", a.repl))
        if sum(a.ways) != args.llc_ways:
            ap.error("+ways=%s adds up to %d, the LLC has %d ways" % (vec_text(a.ways), sum(a.ways), args.llc_ways))
    if a.deg is not None and not uses_ipcp:
        ap.error("+deg needs an IPCP experiment ('%s' has no IPCP prefetcher)" % (spec or "this run"))
    # ---- label = results sub-folder
    if args.label and not multi:
        a.label = args.label
    elif name:
        if name in EQUAL_PRESETS:
            base = name
        elif name == "plain":
            base = a.repl                                    # srrip / lru
        elif EXPERIMENTS[name][2] is not None:
            base = name                                      # ucp, ucp_ipcp_l2, ...
        else:
            base = "%s_%s" % (name, a.repl)                  # ipcp_l2_srrip, ...
        a.label = base + ("_w%s" % ways_str(a.ways) if a.ways else "") + ("_d%s" % vec_text(a.deg) if a.deg else "")
    else:
        a.label = "%s_%s_%s" % (a.l1d_pref, a.l2c_pref, a.repl) + ("_w%s" % ways_str(a.ways) if a.ways else "") \
                  + ("_d%s" % vec_text(a.deg) if a.deg else "")
    # ---- environment of the simulations
    a.env = {}
    if a.equal:
        a.env["UCP_FIXED_ALLOCATION"] = ",".join(str(x) for x in equal_split(args.llc_ways))
    if a.ways:
        a.env["UCP_FIXED_ALLOCATION"] = ",".join(str(x) for x in a.ways)
    if a.deg is not None:
        a.env["IPCP_DEGREE_CAPS"] = ",".join(str(x) for x in a.deg)
    a.ways_text = ("equal" if a.equal else ways_str(a.ways) if a.ways else ("adaptive" if a.repl == "ucp" else "none"))
    a.deg_text = vec_text(a.deg) if a.deg is not None else "original"
    a.knob_text = " ".join("%s=%s" % kv for kv in sorted(a.env.items()))
    return a


def check_hooks(root, a):
    """The knobs are environment variables read by the simulator sources; refuse to run if this tree does not read them
    (the result would silently be the unmodified experiment)."""
    need = []
    if "UCP_FIXED_ALLOCATION" in a.env:
        need.append(("replacement/%s.llc_repl" % a.repl, "UCP_FIXED_ALLOCATION"))
    if "IPCP_DEGREE_CAPS" in a.env:
        for lvl, pref in (("l1d", a.l1d_pref), ("l2c", a.l2c_pref)):
            if pref == IPCP:
                need.append(("prefetcher/%s.%s_pref" % (pref, lvl), "IPCP_DEGREE_CAPS"))
    for rel, var in need:
        f = root / rel
        if not f.is_file() or var not in f.read_text(errors="replace"):
            sys.exit("[ERROR] experiment '%s' needs the hook %s in %s, which this source tree does not have (it comes with the "
                     "'Add LLC4 experiment control hooks for IPCP' commit of the combined-ucp-ipcp branch).  Update the tree, or "
                     "run only experiments without +ways / +deg / eq*." % (a.label, var, rel))


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
HDR_RE = {"warmup_instructions": re.compile(r"^Warmup Instructions:\s*(\d+)", re.M),
          "simulation_instructions": re.compile(r"^Simulation Instructions:\s*(\d+)", re.M),
          "num_cpus": re.compile(r"^Number of CPUs:\s*(\d+)", re.M),
          "llc_set": re.compile(r"^LLC sets:\s*(\d+)", re.M),
          "llc_way": re.compile(r"^LLC ways:\s*(\d+)", re.M)}


def parse_config(text):
    """Simulator set-up printed at the top of every log ('LLC sets: 2048', 'LLC ways: 16', ...)."""
    out = {}
    for k, rx in HDR_RE.items():
        m = rx.search(text)
        out[k] = int(m.group(1)) if m else None
    out["llc_size"] = (out["llc_set"] * out["llc_way"] * 64 // 1024) if out["llc_set"] and out["llc_way"] else None   # KB
    return out


HEADER_END_RE = re.compile(r"^(?:Heartbeat|Warmup complete|Finished)", re.M)


def header_config(text):
    """All 'key value' simulator-setup lines printed before the run starts (core, caches, DRAM; added by the statistics
    hook of --build), so that experiments built from the same sources compare equal."""
    head = HEADER_END_RE.split(text, maxsplit=1)[0]
    cfg = {}
    for m in re.finditer(r"^([a-z][a-z0-9_]*) (\S+)[ \t]*$", head, re.M):
        cfg[m.group(1)] = m.group(2)
    for k, rx in HDR_RE.items():
        mm = rx.search(head)
        if mm and k not in ("warmup_instructions", "simulation_instructions"):
            cfg.setdefault(k, mm.group(1))
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
# Building the binaries (in a throw-away copy of the sources)
# --------------------------------------------------------------------------
def binary_name(args):
    return "%s-no-%s-%s-no-%s-%dcore-llc%ds%dw" % (args.bp, args.l1d_pref, args.l2c_pref, args.repl, NCORES,
                                                    args.llc_sets, args.llc_ways)


SRC_ITEMS = ("Makefile", "build_champsim.sh", "src", "inc", "branch", "prefetcher", "replacement")


def copy_sources(root, tmp):
    for item in SRC_ITEMS:
        src, dst = root / item, tmp / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.o", "*.bak", "*.d"))
        else:
            shutil.copy2(src, dst)


# ---- the statistics hook ---------------------------------------------------------------------------------------
# Everything below is READ-ONLY: it only copies counters that the simulator already keeps (plus one per-core split of
# the miss latency) and prints them in the same  Core_<n>_<stat> <value>  format as the Pythia ChampSim.
HOOK_CACHE_H = """    uint64_t total_miss_latency;
    // ---- statistics hook (read-only)
    uint64_t lat_sum_cpu[NUM_CPUS], lat_cnt_cpu[NUM_CPUS], roi_lat_sum[NUM_CPUS], roi_lat_cnt[NUM_CPUS];
    uint64_t roi_pf_requested, roi_pf_issued, roi_pf_useful, roi_pf_useless, roi_pf_fill;
    // IPCP per-class counters [0=filled,1=useful,2=late][cpu][class]: value when the warm-up ended / when the core finished its region
    uint64_t cls_base[3][NUM_CPUS][6], cls_roi[3][NUM_CPUS][6];
"""

HOOK_ROI_SNAPSHOT = """        cache->roi_miss[cpu][i] = cache->sim_miss[cpu][i];
    }
    // ---- statistics hook: snapshot of the per-core prefetch / latency counters at the end of this core's region of interest
    cache->roi_lat_sum[cpu] = cache->lat_sum_cpu[cpu];
    cache->roi_lat_cnt[cpu] = cache->lat_cnt_cpu[cpu];
    cache->roi_pf_requested = cache->pf_requested;
    cache->roi_pf_issued = cache->pf_issued;
    cache->roi_pf_useful = cache->pf_useful;
    cache->roi_pf_useless = cache->pf_useless;
    cache->roi_pf_fill = cache->pf_fill;
    for (uint32_t j = 0; j < 6; j++) {
        cache->cls_roi[0][cpu][j] = cache->pref_filled[cpu][j];
        cache->cls_roi[1][cpu][j] = cache->pref_useful[cpu][j];
        cache->cls_roi[2][cpu][j] = cache->pref_late[cpu][j];
    }
"""

HOOK_OCC_CODE = r"""
// ----- LLC occupancy: from the end of the warm-up until the last core finishes, the LLC is scanned every
//       LLC_OCC_INTERVAL cycles; owner of a block = the core that filled it (block.cpu)
#define LLC_OCC_INTERVAL @INTERVAL@ULL
static uint64_t occ_next_cycle = 0, occ_samples = 0, occ_sum_valid = 0;
static uint64_t occ_sum_core[NUM_CPUS], occ_sum_pf[NUM_CPUS];

static void llc_occ_scan(uint64_t *own, uint64_t *own_pf, uint64_t &valid)
{
    for (uint32_t c = 0; c < NUM_CPUS; c++) own[c] = own_pf[c] = 0;
    valid = 0;
    for (uint32_t s = 0; s < uncore.LLC.NUM_SET; s++)
        for (uint32_t w = 0; w < uncore.LLC.NUM_WAY; w++) {
            const BLOCK &b = uncore.LLC.block[s][w];
            if (!b.valid) continue;
            valid++;
            if (b.cpu < NUM_CPUS) { own[b.cpu]++; if (b.prefetch) own_pf[b.cpu]++; }
        }
}

static void llc_occ_sample()
{
    uint64_t own[NUM_CPUS], own_pf[NUM_CPUS], valid;
    llc_occ_scan(own, own_pf, valid);
    occ_samples++;
    occ_sum_valid += valid;
    for (uint32_t c = 0; c < NUM_CPUS; c++) { occ_sum_core[c] += own[c]; occ_sum_pf[c] += own_pf[c]; }
}

static void print_llc_occupancy()
{
    uint64_t own[NUM_CPUS], own_pf[NUM_CPUS], valid;
    llc_occ_scan(own, own_pf, valid);
    char buf[200];
    cout << endl << "LLC_occupancy_interval_cycles " << LLC_OCC_INTERVAL << endl
         << "LLC_occupancy_samples " << occ_samples << endl
         << "LLC_occupancy_total_blocks " << (uint64_t)uncore.LLC.NUM_SET * uncore.LLC.NUM_WAY << endl;
    snprintf(buf, sizeof(buf), "LLC_occupancy_avg_valid_blocks %.3f", occ_samples ? (double)occ_sum_valid / occ_samples : 0.0);
    cout << buf << endl << "LLC_occupancy_final_valid_blocks " << valid << endl;
    for (uint32_t c = 0; c < NUM_CPUS; c++) {
        snprintf(buf, sizeof(buf), "Core_%u_LLC_occupancy_avg_blocks %.3f", c, occ_samples ? (double)occ_sum_core[c] / occ_samples : 0.0);
        cout << buf << endl;
        snprintf(buf, sizeof(buf), "Core_%u_LLC_occupancy_avg_prefetch_blocks %.3f", c, occ_samples ? (double)occ_sum_pf[c] / occ_samples : 0.0);
        cout << buf << endl;
        cout << "Core_" << c << "_LLC_occupancy_final_blocks " << own[c] << endl
             << "Core_" << c << "_LLC_occupancy_final_prefetch_blocks " << own_pf[c] << endl;
    }
}
"""

HOOK_MAIN_CODE = r"""
// ===== statistics hook -- read-only, inserted by run_4core_mixes_ipcp.py --build; does not change the simulation
#include <cstdio>
@OCC@
// ----- set-up of the simulated system, printed in the log header ('key value' lines)
static void hook_print_system_config()
{
@SYSCFG@
}

// ----- per-core region-of-interest statistics of one cache level
static void hook_cache_core(uint32_t c, const char *nm, CACHE *k)
{
    uint64_t ta = 0, th = 0, tm = 0;
    for (uint32_t t = 0; t < NUM_TYPES; t++) { ta += k->roi_access[c][t]; th += k->roi_hit[c][t]; tm += k->roi_miss[c][t]; }
    char buf[200];
    cout << "Core_" << c << "_" << nm << "_total_access " << ta << endl;
    cout << "Core_" << c << "_" << nm << "_total_hit " << th << endl;
    cout << "Core_" << c << "_" << nm << "_total_miss " << tm << endl;
    static const char *tn[] = {"load", "rfo", "prefetch", "writeback"};
    for (uint32_t t = 0; t < 4; t++) {
        cout << "Core_" << c << "_" << nm << "_" << tn[t] << "_access " << k->roi_access[c][t] << endl;
        cout << "Core_" << c << "_" << nm << "_" << tn[t] << "_hit " << k->roi_hit[c][t] << endl;
        cout << "Core_" << c << "_" << nm << "_" << tn[t] << "_miss " << k->roi_miss[c][t] << endl;
    }
    cout << "Core_" << c << "_" << nm << "_loads " << k->roi_access[c][0] << endl;      // = _load_access (name used by the Excel script)
    snprintf(buf, sizeof(buf), "Core_%u_%s_average_miss_latency %.3f", c, nm,
             k->roi_lat_cnt[c] ? (double)k->roi_lat_sum[c] / k->roi_lat_cnt[c] : 0.0);
    cout << buf << endl;
}

// ----- prefetch counters of a private cache (L1D / L2C of one core): snapshot at the end of the core's ROI
static void hook_pf_core(uint32_t c, const char *nm, CACHE *k)
{
    cout << "Core_" << c << "_" << nm << "_prefetch_requested " << k->roi_pf_requested << endl;
    cout << "Core_" << c << "_" << nm << "_prefetch_issued " << k->roi_pf_issued << endl;
    cout << "Core_" << c << "_" << nm << "_prefetch_fill " << k->roi_pf_fill << endl;
    cout << "Core_" << c << "_" << nm << "_prefetch_useful " << k->roi_pf_useful << endl;
    cout << "Core_" << c << "_" << nm << "_prefetch_useless " << k->roi_pf_useless << endl;
}

// ----- IPCP per-class counters (class 1 = GS, 2 = CS, 3 = CPLX, 4 = NL; 0 = unclassified).  IPCP's own counters are never reset and
//       keep counting after a core has finished its region, so what is printed is (value when the core finished its region) -
//       (value when its warm-up ended): the core's own measured region, like every other Core_<n>_ statistic.
static uint64_t cls_delta(CACHE &k, uint32_t kind, uint32_t c, uint32_t j)
{
    return k.cls_roi[kind][c][j] >= k.cls_base[kind][c][j] ? k.cls_roi[kind][c][j] - k.cls_base[kind][c][j] : 0;
}

static void hook_ipcp(uint32_t c)
{
    static const char *cn[] = {"none", "GS", "CS", "CPLX", "NL"};
    for (uint32_t k = 0; k < 5; k++) {
        cout << "Core_" << c << "_L1D_IPCP_" << cn[k] << "_filled " << cls_delta(ooo_cpu[c].L1D, 0, c, k) << endl;
        cout << "Core_" << c << "_L1D_IPCP_" << cn[k] << "_useful " << cls_delta(ooo_cpu[c].L1D, 1, c, k) << endl;
        cout << "Core_" << c << "_L1D_IPCP_" << cn[k] << "_late " << cls_delta(ooo_cpu[c].L1D, 2, c, k) << endl;
        cout << "Core_" << c << "_L2C_IPCP_" << cn[k] << "_filled " << cls_delta(ooo_cpu[c].L2C, 0, c, k) << endl;
        cout << "Core_" << c << "_L2C_IPCP_" << cn[k] << "_useful " << cls_delta(ooo_cpu[c].L2C, 1, c, k) << endl;
        cout << "Core_" << c << "_L2C_IPCP_" << cn[k] << "_late " << cls_delta(ooo_cpu[c].L2C, 2, c, k) << endl;
    }
}

static void hook_dram()
{
    uint64_t total_congested_cycle = 0;
    for (uint32_t i = 0; i < DRAM_CHANNELS; i++) {
        cout << "Channel_" << i << "_RQ_row_buffer_hit " << uncore.DRAM.RQ[i].ROW_BUFFER_HIT << endl;
        cout << "Channel_" << i << "_RQ_row_buffer_miss " << uncore.DRAM.RQ[i].ROW_BUFFER_MISS << endl;
        cout << "Channel_" << i << "_WQ_row_buffer_hit " << uncore.DRAM.WQ[i].ROW_BUFFER_HIT << endl;
        cout << "Channel_" << i << "_WQ_row_buffer_miss " << uncore.DRAM.WQ[i].ROW_BUFFER_MISS << endl;
        cout << "Channel_" << i << "_WQ_full " << uncore.DRAM.WQ[i].FULL << endl;
        total_congested_cycle += uncore.DRAM.dbus_cycle_congested[i];
    }
    if (uncore.DRAM.dbus_congested[NUM_TYPES][NUM_TYPES])
        cout << "avg_congested_cycle " << (total_congested_cycle / uncore.DRAM.dbus_congested[NUM_TYPES][NUM_TYPES]) << endl;
}

static void hook_print_stats()
{
    char buf[200];
    cout << endl << "Statistics of the region of interest (statistics hook)" << endl;
    for (uint32_t c = 0; c < NUM_CPUS; c++) {
        snprintf(buf, sizeof(buf), "Core_%u_IPC %.6f", c,
                 ooo_cpu[c].finish_sim_cycle ? (double)ooo_cpu[c].finish_sim_instr / ooo_cpu[c].finish_sim_cycle : 0.0);
        cout << buf << endl;
        cout << "Core_" << c << "_instructions " << ooo_cpu[c].finish_sim_instr << endl;
        cout << "Core_" << c << "_cycles " << ooo_cpu[c].finish_sim_cycle << endl;
        hook_cache_core(c, "L1D", &ooo_cpu[c].L1D);
        hook_cache_core(c, "L2C", &ooo_cpu[c].L2C);
        hook_cache_core(c, "LLC", &uncore.LLC);
        hook_pf_core(c, "L1D", &ooo_cpu[c].L1D);
        hook_pf_core(c, "L2C", &ooo_cpu[c].L2C);
        hook_ipcp(c);
    }
    hook_dram();
@OCC_PRINT@}
// ===== end of statistics hook

"""

SYSCFG_CORE = ("ROB_SIZE", "LQ_SIZE", "SQ_SIZE", "FETCH_WIDTH", "DECODE_WIDTH", "EXEC_WIDTH", "RETIRE_WIDTH",
               "SCHEDULER_SIZE", "BRANCH_MISPREDICT_PENALTY", "DRAM_CHANNELS", "DRAM_RANKS", "DRAM_BANKS", "DRAM_ROWS",
               "DRAM_COLUMNS", "DRAM_IO_FREQ")
SYSCFG_CACHE = ["%s_%s" % (l, s) for l in ("L1I", "L1D", "L2C", "LLC")
                for s in ("SET", "WAY", "RQ_SIZE", "WQ_SIZE", "PQ_SIZE", "MSHR_SIZE", "LATENCY")]


def edit_once(path, edits, what):
    """Apply (old, new, count) edits to a file; every anchor must occur exactly `count` times."""
    txt = path.read_text()
    for old, new, count in edits:
        if txt.count(old) != count:
            sys.exit("[ERROR] cannot add the %s: expected %d x '%s' in %s (found %d). This simulator version is not the "
                     "one the script was written for; run with --no-hook to build without the statistics hook (the "
                     "Excel script then cannot read the logs)." % (what, count, old.strip()[:60], path.name, txt.count(old)))
        txt = txt.replace(old, new)
    path.write_text(txt)


def patch_stats_hook(tmp, args, tag):
    defined = set()
    for h in (tmp / "inc").glob("*.h"):
        defined |= set(re.findall(r"^#define\s+([A-Z0-9_]+)\b", h.read_text(), re.M))
    lines = []
    for m in SYSCFG_CORE + tuple(SYSCFG_CACHE):
        if m in defined:
            lines.append('    cout << "%s " << (%s) << endl;' % (m.lower(), m))
    lines.append('    cout << "llc_size_kb " << ((uint64_t)LLC_SET * LLC_WAY * BLOCK_SIZE / 1024) << endl;')
    lines.append('    cout << "num_cpus_built " << NUM_CPUS << endl;')
    occ = "" if args.no_occupancy else HOOK_OCC_CODE.replace("@INTERVAL@", str(int(args.occupancy_interval)))
    code = (HOOK_MAIN_CODE.replace("@OCC@", occ).replace("@SYSCFG@", "\n".join(lines))
            .replace("@OCC_PRINT@", "" if args.no_occupancy else "    print_llc_occupancy();\n"))
    edit_once(tmp / "inc/cache.h", [("    uint64_t total_miss_latency;\n", HOOK_CACHE_H, 1)], "statistics hook (inc/cache.h)")
    edit_once(tmp / "src/cache.cc",
              [("total_miss_latency += current_miss_latency;",
                "total_miss_latency += current_miss_latency; lat_sum_cpu[fill_cpu] += current_miss_latency; lat_cnt_cpu[fill_cpu]++;", 2)],
              "statistics hook (src/cache.cc)")
    sample = ("" if args.no_occupancy else
              "        if (all_warmup_complete > NUM_CPUS && global_cycle_counter >= occ_next_cycle) { llc_occ_sample(); "
              "occ_next_cycle = global_cycle_counter + LLC_OCC_INTERVAL; }\n")
    edit_once(tmp / "src/main.cc", [
        ("        cache->roi_miss[cpu][i] = cache->sim_miss[cpu][i];\n    }\n", HOOK_ROI_SNAPSHOT, 1),
        ("    cache->total_miss_latency = 0;\n",
         "    cache->total_miss_latency = 0;\n    cache->lat_sum_cpu[cpu] = 0;\n    cache->lat_cnt_cpu[cpu] = 0;\n"
         "    for (uint32_t j = 0; j < 6; j++) {\n"
         "        cache->cls_base[0][cpu][j] = cache->pref_filled[cpu][j];\n"
         "        cache->cls_base[1][cpu][j] = cache->pref_useful[cpu][j];\n"
         "        cache->cls_base[2][cpu][j] = cache->pref_late[cpu][j];\n    }\n", 1),
        ("int main(int argc, char** argv)", code + "int main(int argc, char** argv)", 1),
        ('    cout << "LLC ways: " << LLC_WAY << endl;\n', '    cout << "LLC ways: " << LLC_WAY << endl;\n    hook_print_system_config();\n', 1),
        ("        if (all_warmup_complete > NUM_CPUS)\n            global_cycle_counter++;\n",
         "        if (all_warmup_complete > NUM_CPUS)\n            global_cycle_counter++;\n" + sample, 1),
        ("    print_dram_stats();\n", "    print_dram_stats();\n    hook_print_stats();\n", 1)],
        "statistics hook (src/main.cc)")
    print("[BUILD %s] added the read-only statistics hook%s" % (tag, "" if args.no_occupancy else
          " (LLC occupancy sampled every %d cycles after warm-up)" % args.occupancy_interval))


def patch_ucp_final_stats(tmp, args, tag):
    """UCP: print the number of completed partitions and the final way quotas at the end of the log."""
    f = tmp / "replacement" / ("%s.llc_repl" % args.repl)
    txt = f.read_text()
    if "static uint64_t partition_count = 0;" not in txt or "fclose(output_Stats);" not in txt:
        print("[BUILD %s] note: no UCP partition counter found in %s -> no '[UCP] completed partitions' line" % (tag, f.name))
        return
    txt = txt.replace("static uint64_t partition_count = 0;", "static uint64_t &partition_count = ucp_hook_partitions;", 1)
    txt = txt.replace("vector<uint32_t> ways_of_cpu(NUM_CPUS);",
                      "uint64_t ucp_hook_partitions = 0;   // statistics hook\nvector<uint32_t> ways_of_cpu(NUM_CPUS);", 1)
    txt = txt.replace("fclose(output_Stats);",
                      'printf("\\n[UCP] completed partitions=%lu last quotas:", (unsigned long)ucp_hook_partitions);\n'
                      '    for (uint32_t i = 0; i < NUM_CPUS; i++) printf(" CPU%u=%u", i, ways_of_cpu[i]);\n'
                      '    printf("\\n");\n    fclose(output_Stats);', 1)
    if "ucp_hook_partitions = 0" not in txt:
        print("[BUILD %s] note: could not add the UCP quota line" % tag)
        return
    f.write_text(txt)


def build_binary(root, args):
    tag = "%s+%s/%s" % (args.l1d_pref, args.l2c_pref, args.repl)
    for need in SRC_ITEMS:
        if not (root / need).exists():
            sys.exit("[ERROR] %s/%s not found -- is --root the ChampSim repository?" % (root, need))
    for rel in ("replacement/%s.llc_repl" % args.repl, "branch/%s.bpred" % args.bp, "prefetcher/no.l1i_pref",
                "prefetcher/no.llc_pref", "prefetcher/%s.l1d_pref" % args.l1d_pref, "prefetcher/%s.l2c_pref" % args.l2c_pref):
        if not (root / rel).is_file():
            sys.exit("[ERROR] %s not found in %s" % (rel, root))
    tmp = Path(tempfile.mkdtemp(prefix="champsim_llc%ds%dw_" % (args.llc_sets, args.llc_ways)))
    print("[BUILD %s] building in a temporary copy: %s (your repository is not modified)" % (tag, tmp), flush=True)
    try:
        copy_sources(root, tmp)
        # --- fix the LLC geometry (only in the copy)
        cache_h = tmp / "inc/cache.h"
        txt = cache_h.read_text()
        txt, n1 = re.subn(r"^#define\s+LLC_SET\b.*$", "#define LLC_SET %d" % args.llc_sets, txt, flags=re.M)
        txt, n2 = re.subn(r"^#define\s+LLC_WAY\b.*$", "#define LLC_WAY %d" % args.llc_ways, txt, flags=re.M)
        if n1 != 1 or n2 != 1:
            sys.exit("[ERROR] could not find single '#define LLC_SET/LLC_WAY' lines in inc/cache.h")
        cache_h.write_text(txt)
        if not args.no_hook:
            patch_stats_hook(tmp, args, tag)
        if args.repl == "ucp" and not args.no_hook:
            patch_ucp_final_stats(tmp, args, tag)
        env = dict(os.environ, TERM=os.environ.get("TERM", "xterm"), MAKEFLAGS="-j%d" % max(1, args.build_jobs))
        log = root / RESULTS_ROOT / "_build" / ("build_%s.log" % binary_name(args))
        log.parent.mkdir(parents=True, exist_ok=True)
        print("[BUILD %s] compiling %s (log: %s) ..." % (tag, binary_name(args), log), flush=True)
        with open(log, "w") as lf:
            rc = subprocess.call(["./build_champsim.sh", args.bp, "no", args.l1d_pref, args.l2c_pref, "no", args.repl, str(NCORES)],
                                 cwd=str(tmp), env=env, stdout=lf, stderr=subprocess.STDOUT)
        built = tmp / "bin" / ("%s-no-%s-%s-no-%s-%dcore" % (args.bp, args.l1d_pref, args.l2c_pref, args.repl, NCORES))
        if rc != 0 or not built.is_file():
            print("".join(log.read_text().splitlines(True)[-25:]))
            sys.exit("[ERROR] build failed (see %s)" % log)
        dest = root / "bin" / binary_name(args)
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        print("[BUILD %s] done: %s" % (tag, dest), flush=True)
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


def build_cmd(binary, root, args, traces, warm, sim):
    return ([str(binary), "-warmup_instructions", str(warm), "-simulation_instructions", str(sim)]
            + args.extra_opts + ["-traces"] + traces)


def probe_binary(binary, root, args, traces):
    """Start the binary on tiny runs just long enough to read its configuration."""
    cmd = build_cmd(binary, root, args, traces, 1000, 1000)
    p = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True, env=dict(os.environ, **args.env))
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


PRINT_LOCK = threading.Lock()           # shared by all runners: lines of different experiments never get mixed up


class Runner:
    def __init__(self, binary, root, args, warm, sim, trace_dir, results):
        self.binary, self.root, self.args = binary, root, args
        self.warm, self.sim, self.trace_dir, self.results = warm, sim, trace_dir, Path(results)
        self.lock, self.procs, self.stop = threading.Lock(), {}, threading.Event()
        self.tag = ""                  # '[label] ' prefix of the console lines when several experiments run together

    def mix_dir(self, m):
        return self.results / m.name

    def final_log(self, m):
        return self.mix_dir(m) / ("%s.txt" % m.name)

    def is_done(self, m):
        f = self.final_log(m)
        return f.is_file() and parse_log(f)["complete"]

    def say(self, msg):
        with PRINT_LOCK:
            print("%s %s%s" % (datetime.now().strftime("%H:%M:%S"), self.tag, msg), flush=True)

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
        env_txt = " ".join("%s=%s" % kv for kv in sorted(self.args.env.items()))
        (mdir / "command.txt").write_text((env_txt + " " if env_txt else "") + " ".join(cmd) + "\n")
        tmp_log, final = mdir / "run.running", self.final_log(m)
        self.say("[START] %s" % m.name)
        t0 = time.time()
        with open(tmp_log, "w") as lf:
            # first line of every log: which experiment / knobs produced it (the simulator output follows)
            lf.write("# run_4core_mixes_ipcp.py: experiment=%s ways=%s degree=%s env=%s\n"
                     % (self.args.label, self.args.ways_text, self.args.deg_text, env_txt or "-"))
            lf.flush()
            p = subprocess.Popen(cmd, cwd=str(self.root), stdout=lf, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=dict(os.environ, **self.args.env))
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
                                  re.findall(r"^CPU \d+ runs (\S+)", f.read_text(errors="replace"), re.M))
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
def print_experiments(args):
    print("Presets (L1D prefetcher / L2C prefetcher / LLC policy):")
    for k, (l1d, l2c, repl) in EXPERIMENTS.items():
        print("  %-15s %-5s / %-5s / %s%s" % (k, "IPCP" if l1d == IPCP else "-", "IPCP" if l2c == IPCP else "-",
                                          "UCP (equal split pinned)" if k in EQUAL_PRESETS else "UCP" if repl else "plain (SRRIP, --repl lru)",
                                          "" if k in ALL_EXPERIMENTS else "   (extra)" if k in ("ucp_ipcp_l1", "ipcp_l1") else ""))
    degs = [d for d in str(args.degrees).split(",") if d]
    print("Groups:\n  all         %s\n  grid        %s\n  degrees     %s\n  everything  all + grid + degrees"
          % (", ".join(ALL_EXPERIMENTS), ", ".join(GRID_EXPERIMENTS), ", ".join("%s+deg=%s" % (b, d) for b in DEGREE_BASES for d in degs)))
    print("Knobs:  +ways=A-B-C-D (UCP way split, sum %d) | +ways=equal | +deg=N or +deg=N0-N1-N2-N3 (IPCP degree bound, 0 = off)" % args.llc_ways)
    print("Examples:  --experiment grid | --experiment degrees | --experiment ucp_ipcp_l1_l2+deg=2,ucp+ways=2-2-6-6")


def main():
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)      # '--list | head' must not print a traceback
    here = Path(__file__).resolve().parent
    default_root = here.parent if here.name == "scripts" else here
    ap = argparse.ArgumentParser(
        description="Run 50 four-core mixes (class-balanced by cache/prefetch sensitivity) for the UCP / IPCP experiments, in parallel.",
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
    g.add_argument("--label", default=None, help="experiment name = results sub-folder (default: the experiment name; "
                   "<experiment>_<repl> for the IPCP-alone experiments, e.g. ipcp_l2_srrip, ipcp_l1_l2_lru)")
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
                   "from the prefetchers/--repl/--bp, e.g. bin/perceptron-no-ipcp_isca2020-ipcp_isca2020-no-ucp-4core-llc2048s16w")
    b.add_argument("--experiment", default=None, metavar="SPEC[,SPEC...]",
                   help="preset(s): ucp = UCP, no prefetcher | ucp_ipcp_l2 = UCP + IPCP at L2C | ucp_ipcp_l1_l2 = UCP + IPCP "
                        "at L1D and L2C | ipcp_l2 / ipcp_l1_l2 = IPCP ALONE on a plain LLC (SRRIP unless --repl says otherwise) | "
                        "plain = plain LLC, no prefetcher | eq = UCP's LLC pinned at the equal split, no prefetcher | "
                        "eq_ipcp_l1_l2 / eq_ipcp_l2 = equal split + IPCP | extra: ucp_ipcp_l1, ipcp_l1, eq_ipcp_l1.  Groups: all (the "
                        "five original experiments), grid (ucp, ucp_ipcp_l1_l2, ipcp_l1_l2, plain, eq, eq_ipcp_l1_l2), degrees "
                        "({ucp_ipcp_l1_l2, eq_ipcp_l1_l2, ipcp_l1_l2} x --degrees), everything.  A preset can carry knobs: "
                        "ucp_ipcp_l1_l2+deg=2, ucp+ways=2-2-6-6.  Several experiments run in ONE command and share the --jobs "
                        "threads (--list-experiments shows everything)")
    b.add_argument("--degrees", default=DEFAULT_DEGREES, help="IPCP degree bounds swept by --experiment degrees / everything")
    b.add_argument("--ways", dest="ways_all", default=None, metavar="A-B-C-D",
                   help="pin the UCP way allocation of every UCP experiment of this command at this split (same as +ways=)")
    b.add_argument("--degree", dest="degree_all", default=None, metavar="N[-N-N-N]",
                   help="bound the IPCP degree of every IPCP experiment of this command (same as +deg=)")
    b.add_argument("--list-experiments", action="store_true", help="print the presets, groups and knob syntax and exit")
    b.add_argument("--l1d-pref", dest="l1d_pref", default=None, help="L1D prefetcher (prefetcher/<name>.l1d_pref); set by --experiment")
    b.add_argument("--l2c-pref", dest="l2c_pref", default=None, help="L2C prefetcher (prefetcher/<name>.l2c_pref); set by --experiment")
    b.add_argument("--repl", default=None,
                   help="LLC replacement policy (replacement/<name>.llc_repl).  Set by --experiment (ucp for the UCP "
                        "experiments); for the IPCP-alone experiments the default is srrip, use --repl lru for LRU")
    b.add_argument("--bp", default="perceptron", help="branch predictor (branch/<name>.bpred); same as the Pythia runs")
    b.add_argument("--build", action="store_true", help="(re)build the binary with the fixed LLC geometry first")
    b.add_argument("--build-only", action="store_true", help="build and exit")
    b.add_argument("--build-jobs", type=int, default=os.cpu_count() or 4, help="make -j for --build")
    b.add_argument("--no-hook", action="store_true",
                   help="--build: do NOT add the statistics hook (the logs then lack the Core_<n>_* statistics and cannot be "
                        "used by make_ucp_vs_ipcp_excel.py)")
    b.add_argument("--keep-build-dir", action="store_true", help="keep the temporary build directory")
    b.add_argument("--no-occupancy", action="store_true",
                   help="--build: do NOT add the LLC-occupancy sampling (by default the statistics hook samples the LLC blocks "
                        "owned by each core; it does not change any simulated result)")
    b.add_argument("--occupancy-interval", type=int, default=10000,
                   help="--build: LLC occupancy is sampled every this many cycles after warm-up")
    b.add_argument("--extra-opts", default="", help="extra simulator options, e.g. '-low_bandwidth'")
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
    if args.list_experiments:
        print_experiments(args)
        return
    if args.experiment:
        specs = expand_specs(args.experiment, args, ap)
        if len(specs) > 1:
            for opt, flag in (("binary", "--binary"), ("label", "--label"), ("results_dir", "--results-dir"),
                              ("l1d_pref", "--l1d-pref"), ("l2c_pref", "--l2c-pref")):
                if getattr(args, opt):
                    ap.error("%s cannot be combined with several experiments (every experiment has its own binary / folder)" % flag)
            if args.repl == "ucp":
                ap.error("with several experiments, --repl is the LLC policy of the plain-LLC runs (ipcp_*, plain): srrip (default) or lru")
        exp_args = [make_experiment(args, sp, ap, len(specs) > 1) for sp in specs]
        labels = [a.label for a in exp_args]
        dup = {x for x in labels if labels.count(x) > 1}
        if dup:
            ap.error("these experiments would write to the same results folder: %s" % ", ".join(sorted(dup)))
    elif not (args.l1d_pref or args.l2c_pref or args.repl) and (args.list or args.write_mixes):
        exp_args = []                                   # --list / --write-mixes need no experiment
    else:
        exp_args = [make_experiment(args, None, ap, False)]
    multi = len(exp_args) > 1
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

    for a in exp_args:
        check_hooks(root, a)

    # ---- binaries (one per L1D/L2C prefetcher + LLC policy; built in parallel, each in its own temporary copy)
    if args.build or args.build_only:
        if args.binary:
            sys.exit("[ERROR] --build creates its own binary name; do not combine with --binary")
        todo_b = {}
        for a in exp_args:
            todo_b.setdefault(binary_name(a), a)
        each = max(1, args.build_jobs // len(todo_b))
        with ThreadPoolExecutor(max_workers=len(todo_b)) as bp:
            futs_b = []
            for a in todo_b.values():
                b = copy.copy(a)
                b.build_jobs = each
                futs_b.append(bp.submit(build_binary, root, b))
            for fb in futs_b:
                fb.result()
        if args.build_only:
            return
    for a in exp_args:
        a.binary_path = resolve_binary(root, a.binary or str(Path("bin") / binary_name(a)))
        a.results_path = (args.results_dir or root / RESULTS_ROOT / a.label).resolve()

    if args.summarize_only:
        for a in exp_args:
            a.results_path.mkdir(parents=True, exist_ok=True)
            path, rows = write_summary(a.results_path, mixes, a, warm, sim, {})
            print("%ssummary written: %s  (%d of %d mixes have logs)" % (a.label + ": " if multi else "", path,
                                                                        sum(r["status"] != "NOT RUN" for r in rows), len(rows)))
        return

    if not args.dry_run:
        lacking = [a for a in exp_args if not a.binary_path.is_file() or not os.access(a.binary_path, os.X_OK)]
        if lacking:
            sys.exit("[ERROR] binary not found: %s\n  Build %s with:  python3 %s %s--build --build-only"
                     % (", ".join(sorted({str(a.binary_path) for a in lacking})),
                        "them" if len({binary_name(a) for a in lacking}) > 1 else "it", Path(__file__).name,
                        "--experiment %s " % (args.experiment or lacking[0].label)))

    for a in exp_args:
        check_hooks(root, a)

    # ---- traces
    need = sorted({t for m in selected for t in m.traces})
    missing = [t for t in need if not (trace_dir / ("%s.champsimtrace.xz" % t)).is_file()]
    if missing and args.dry_run:
        print("[NOTE] %d trace file(s) not found in %s (dry run only prints the commands)" % (len(missing), trace_dir))
    elif missing:
        sys.exit("[ERROR] %d trace file(s) missing in %s:\n  %s" % (len(missing), trace_dir, "\n  ".join(missing)))

    # ---- one runner per experiment; verify the LLC geometry of every binary BEFORE launching hours of simulation
    plan = []                                            # (runner, experiment args, mixes still to run, #already done)
    for a in exp_args:
        a.results_path.mkdir(parents=True, exist_ok=True)
        write_mixes_csv(a.results_path / "mixes.csv", mixes, trace_dir, traces)
        runner = Runner(a.binary_path, root, a, warm, sim, trace_dir, a.results_path)
        if multi:
            runner.tag = "[%s] " % a.label
        if not args.no_verify and not args.dry_run:
            probe_traces = [str(trace_dir / ("%s.champsimtrace.xz" % t)) for t in selected[0].traces]
            cfg = probe_binary(a.binary_path, root, a, probe_traces)
            bad = check_geometry(cfg, a)
            if cfg.get("llc_set") is None:
                sys.exit("[ERROR] could not read the LLC configuration from %s (is it a ChampSim binary built "
                         "by build_champsim.sh?). Use --no-verify to skip this check." % a.binary_path.name)
            print("[CHECK] %s: cores=%s  LLC=%s sets x %s ways (%s KB)  -> %s" % (
                a.binary_path.name, cfg["num_cpus"], cfg["llc_set"], cfg["llc_way"], cfg["llc_size"],
                "OK" if not bad else "MISMATCH"))
            if bad:
                sys.exit("[ERROR] this binary does not have the required geometry: %s\n"
                         "  Build the right one with:  python3 %s %s--build --build-only%s"
                         % ("; ".join(bad), Path(__file__).name, "--experiment %s " % (args.experiment or a.label),
                            ""))
        todo_a = [m for m in selected if args.force or not runner.is_done(m)]
        plan.append((runner, a, todo_a, len(selected) - len(todo_a)))

    total_todo = sum(len(t) for _, _, t, _ in plan)
    for runner, a, todo_a, skipped in plan:
        with open(a.results_path / "run_manifest.txt", "a") as mf:
            mf.write("==== %s on %s\ncommand : %s\nlabel   : %s\nbinary  : %s\nrepl    : %s\nl1d_pref: %s\nl2c_pref: %s %s\nways    : %s\ndegree  : %s\nenv     : %s\n"
                     "warmup  : %g M, sim: %g M per core\n"
                     "LLC     : %d sets x %d ways\njobs    : %d%s\nmix seed: %d, margin %.2f\nmixes   : %s\n\n" % (
                         datetime.now().isoformat(timespec="seconds"), socket.gethostname(), " ".join(sys.argv),
                         a.label, a.binary_path, a.repl, a.l1d_pref, a.l2c_pref, " ".join(a.extra_opts), a.ways_text, a.deg_text, a.knob_text or "-",
                         args.warmup, args.sim, args.llc_sets,
                         args.llc_ways, args.jobs, " (shared with the other experiments of --experiment all)" if multi else "",
                         args.seed, args.margin, ",".join(str(m.id) for m in selected)))
        print("Experiment: %s  (LLC policy: %s, L1D prefetcher: %s, L2C prefetcher: %s, ways: %s, IPCP degree: %s)\nBinary : %s\nResults: %s\nMixes  : %d selected, %d already done, %d to run | "
              "jobs=%d%s | warm-up %gM + sim %gM per core | LLC %dx%d"
              % (a.label, a.repl, a.l1d_pref, a.l2c_pref, a.ways_text, a.deg_text, a.binary_path, a.results_path, len(selected), skipped, len(todo_a), args.jobs,
                 " (shared by all experiments)" if multi else "", args.warmup, args.sim, args.llc_sets, args.llc_ways))
    if multi:
        print("Total: %d simulations to run with %d threads (mixes are interleaved over the experiments, so all "
              "experiments advance together)" % (total_todo, args.jobs))

    if args.dry_run:
        for runner, a, todo_a, _ in plan:
            for m in todo_a:
                traces_ = [str(trace_dir / ("%s.champsimtrace.xz" % t)) for t in m.traces]
                print("\n# %s%s\n%s%s > %s" % (a.label + " / " if multi else "", m.name, " ".join("%s=%s" % kv for kv in sorted(a.env.items())) + " " if a.env else "",
                                                 " ".join(build_cmd(a.binary_path, root, a, traces_, warm, sim)),
                                                 a.results_path / ("%s.txt" % m.name)))
        return

    walls = {a.label: {} for a in exp_args}
    outcome = {a.label: Counter() for a in exp_args}
    runner_of = {a.label: runner for runner, a, _, _ in plan}

    def _stop(*_):
        raise KeyboardInterrupt
    # explicit handlers: also works when the shell started us in the background (where SIGINT is ignored)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    t_start = time.time()
    # ONE thread pool for everything; jobs are interleaved (mix 1 of every experiment, then mix 2, ...) so that an
    # interrupted run leaves all experiments with the same mixes done, which is what the comparison needs.
    jobs = []
    for k in range(max([len(t) for _, _, t, _ in plan] or [0])):
        for runner, a, todo_a, _ in plan:
            if k < len(todo_a):
                jobs.append((a.label, runner, todo_a[k]))
    pool = ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futs = {pool.submit(runner.run_mix, m): lab for lab, runner, m in jobs}
        for fu in as_completed(futs):
            lab = futs[fu]
            m, status, wall = fu.result()
            walls[lab][m.id] = wall
            outcome[lab][status.split("(")[0]] += 1
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] stopping running simulations ...", flush=True)
        for r in runner_of.values():
            r.kill_all()          # queued mixes see the stop flag and return immediately
        pool.shutdown(wait=True)
        for a in exp_args:
            write_summary(a.results_path, mixes, a, warm, sim, walls[a.label])
        sys.exit(130)
    pool.shutdown(wait=True)

    failed = False
    print("\nFinished in %.1f min" % ((time.time() - t_start) / 60))
    for a in exp_args:
        path, rows = write_summary(a.results_path, mixes, a, warm, sim, walls[a.label])
        pre = "[%s] " % a.label if multi else ""
        print("%s%s" % (pre, dict(outcome[a.label]) or "nothing to run"))
        bad_cfg = [r["mix_name"] for r in rows if r["status"] == "OK" and r["config_ok"] != "yes"]
        if bad_cfg:
            print("%s[WARN] LLC/length configuration differs from the request in: %s" % (pre, ", ".join(bad_cfg)))
        hashes = Counter(r["setup_hash"] for r in rows if r["status"] == "OK" and r.get("setup_hash"))
        if len(hashes) == 1:
            r0 = next(r for r in rows if r.get("setup_hash"))
            print("%sSimulator setup fingerprint: %s (identical in all %d logs; LLC in the logs: %s sets x %s ways)" % (
                pre, next(iter(hashes)), sum(hashes.values()), r0["llc_sets"], r0["llc_ways"]))
        elif hashes:
            print("%s[WARN] logs in this folder come from DIFFERENT simulator setups: %s" % (pre, dict(hashes)))
        print("%sSummary: %s" % (pre, path))
        failed = failed or any(k != "OK" for k in outcome[a.label])
    if multi:
        print("\nNext:  python3 %s --compare-setups %s\n       python3 make_ucp_vs_ipcp_excel.py"
              % (Path(__file__).name, " ".join(str(a.results_path) for a in exp_args)))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()