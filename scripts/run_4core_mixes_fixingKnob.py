#!/usr/bin/env python3
"""
run_4core_mixes_fixingKnob.py  --  50 four-core workload mixes, run in parallel
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

The three experiments (same LLC 2048 x 16, same 50 mixes, same warm-up / simulation length)
    --experiment ucp          UCP partitioning, no prefetcher            -> results_4core_mixes/ucp
    --experiment ucp_pythia   UCP partitioning + Pythia (L2C)            -> results_4core_mixes/ucp_pythia
    --experiment pythia       Pythia ALONE on a plain, non-partitioned   -> results_4core_mixes/pythia_srrip
                              LLC.  Replacement policy = SRRIP by default;
                              use  --repl lru  for LRU                   -> results_4core_mixes/pythia_lru
  (the older spelling  --repl ucp --mode none / pythia  still works exactly as before)

ALL THREE IN ONE COMMAND (multi-threaded)
    python3 scripts/run_4core_mixes.py --experiment all --build --jobs 32        # build both binaries, run 3 x 50 mixes
    python3 scripts/run_4core_mixes.py --experiment all --jobs 32                # binaries already built
    python3 scripts/run_4core_mixes.py --experiment all --repl lru --jobs 32     # Pythia alone with an LRU LLC instead of SRRIP
  --jobs is the TOTAL number of simulations running at the same time (threads); all 150 simulations (ucp, ucp_pythia,
  pythia x 50 mixes) share that one pool, interleaved mix by mix, so the three experiments advance together.  Each
  experiment still gets its own results folder / summary / manifest, mixes that already have a complete log are
  skipped (--force redoes them), --only / --dry-run / --summarize-only / --timeout-hours work as usual, and Ctrl-C
  stops everything cleanly.  --binary, --label, --results-dir and --mode cannot be combined with 'all'.

  UCP-only and UCP+Pythia use the SAME binary (bin/..-ucp-4core-llc2048s16w); only the prefetcher
  configuration differs.  Pythia-alone needs its own binary (bin/..-srrip-4core-llc2048s16w, or ..-lru-..),
  because the LLC replacement policy is compiled in; --build creates it with the identical 2048 x 16 LLC.
  Pythia is configured identically in "ucp_pythia" and "pythia" (config/pythia.ini), so the only
  difference between those two experiments is UCP partitioning vs. a plain SRRIP/LRU LLC.
  After running experiments, check that their simulator setups match:
    python3 scripts/run_4core_mixes.py --compare-setups results_4core_mixes/ucp results_4core_mixes/ucp_pythia results_4core_mixes/pythia_srrip

KNOB EXPERIMENTS -- fixed LLC ways and a fixed Pythia degree (for the coordination study)
  Two knobs can be set per experiment, without touching your repository:
      +ways=A-B-C-D         the UCP way allocation is FROZEN at this split (A+B+C+D = LLC ways, each >= 1); +ways=equal = 4-4-4-4
      +deg=N                upper bound of the Pythia prefetch degree of every core;  +deg=N0-N1-N2-N3 = one bound per core.
                            Pythia's own adaptive degree is 1..6, so +deg=1 is its least aggressive setting and 0 switches
                            the L2C prefetcher of that core off.  Without +deg the original Pythia runs unchanged.
  Presets (the first three are the original ones)
      ucp          UCP, no prefetcher                      -> results_4core_mixes/ucp
      ucp_pythia   UCP + Pythia                            -> results_4core_mixes/ucp_pythia
      pythia       Pythia on a plain LLC (SRRIP/--repl)    -> results_4core_mixes/pythia_srrip
      plain        plain LLC (SRRIP/--repl), NO prefetcher -> results_4core_mixes/srrip
      eq           UCP's way-owned LLC frozen at the equal split 4/4/4/4, no prefetcher   -> results_4core_mixes/eq
      eq_pythia    the same equal split + Pythia                                          -> results_4core_mixes/eq_pythia
  Groups (comma separated lists are fine:  --experiment ucp,eq_pythia+deg=2)
      all          ucp, ucp_pythia, pythia                                    (as before)
      grid         ucp, ucp_pythia, pythia, plain, eq, eq_pythia   = {plain LLC, UCP, equal split} x {no prefetcher, Pythia}
      degrees      {ucp_pythia, eq_pythia, pythia} x --degrees (default 1,2,4)  -> e.g. results_4core_mixes/ucp_pythia_d2
      everything   grid + degrees
  Result folders get a suffix for the knobs:  _w2-2-6-6 (ways)  _d2 (same degree everywhere)  _d0-4-4-0 (per core).
  Examples
      python3 run_4core_mixes.py --experiment grid --build --jobs 32
      python3 run_4core_mixes.py --experiment degrees --build --jobs 32
      python3 run_4core_mixes.py --experiment ucp_pythia+deg=0-4-4-0,ucp+ways=2-2-6-6 --build --jobs 32
      python3 run_4core_mixes.py --list-experiments                       # presets, groups, knob syntax
  How the knobs reach the simulator -- your repository is never modified, there is nothing to restore
      * equal split: the environment variable UCP_PARTITION_INTERVAL (already read by your replacement/ucp.llc_repl) is set to
        a huge value for the run, so the initial equal split is never changed.  This runs on the ORIGINAL binary.
      * +ways=<other split> and +deg=...: --build compiles a second binary "<original name>-knob" from a throw-away copy
        of the sources in which (1) replacement/ucp.llc_repl also reads UCP_FIXED_ALLOCATION="a,b,c,d" and (2) the Pythia L2C
        wrapper also reads PYTHIA_DEGREE_CAPS="d0,d1,d2,d3" (+ one member in the Scooby class that clamps its degree).  With
        those variables unset the knob binary behaves like the original one.
      * the original binaries are always built from the unpatched sources and every experiment without +ways / +deg runs on
        them, so ucp / ucp_pythia / pythia keep the original logic.  The knobs are recorded in the first line of every
        log, in command.txt and in run_manifest.txt (ways / degree / env).

LLC occupancy (per core and for the whole LLC)
  --build adds a small READ-ONLY statistics hook to the temporary build copy (your sources are not touched).  From
  the end of the warm-up until the last core finishes, the LLC is scanned every --occupancy-interval cycles (default
  10000) and the log ends with   Core_<n>_LLC_occupancy_avg_blocks  (valid LLC blocks owned by core n, averaged),
  ..._avg_prefetch_blocks (of those: prefetched and not used yet), ..._final_blocks / ..._final_prefetch_blocks (snapshot
  at the end) plus LLC_occupancy_total_blocks / _avg_valid_blocks.  Simulated results (IPC, misses, ...) are identical
  to a build without the hook (--no-occupancy).  Binaries built earlier have no occupancy lines: rebuild once with --build
  and re-run the mixes whose occupancy you want (make_ucp_vs_pythia_excel.py shows it for every experiment that has it).

Quick start (from the repo root, or anywhere)
    python3 scripts/run_4core_mixes.py --list                      # show the 50 mixes
    python3 scripts/run_4core_mixes.py --build --build-only        # build the UCP binary, LLC 2048 x 16
    python3 scripts/run_4core_mixes.py --experiment ucp --jobs 8
    python3 scripts/run_4core_mixes.py --experiment ucp_pythia --jobs 8
    python3 scripts/run_4core_mixes.py --experiment pythia --build --build-only    # build the SRRIP binary (once)
    python3 scripts/run_4core_mixes.py --experiment pythia --jobs 8                # Pythia alone, SRRIP LLC
    python3 scripts/run_4core_mixes.py --experiment pythia --repl lru --build --jobs 8   # ... or with an LRU LLC
    python3 scripts/run_4core_mixes.py --only 1-6,40               # run a subset
    python3 scripts/run_4core_mixes.py --summarize-only            # rebuild summary.csv from the logs

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
LLC_SETS = 2048                        # required LLC sets  (one LLC shared by the 4 cores)
LLC_WAYS = 16                          # required LLC ways
RESULTS_ROOT = "results_4core_mixes"   # under the repo root; one sub-folder per experiment
# ============================================================================

# --experiment presets: (LLC replacement policy, prefetch mode).  A policy of None = "default, can be overridden with
# --repl" (a plain, non-partitioned LLC).  "pythia" is Pythia ALONE; "plain" is the same LLC with NO prefetcher; "eq" /
# "eq_pythia" use UCP's way-owned LLC with the way allocation frozen at the equal split (4/4/4/4 for 16 ways).
EXPERIMENTS = {"ucp": ("ucp", "none"),
               "ucp_pythia": ("ucp", "pythia"),
               "pythia": (None, "pythia"),
               "plain": (None, "none"),
               "eq": ("ucp", "none"),
               "eq_pythia": ("ucp", "pythia"),
               # coord-controller tree: DPC-3 IPCP (prefetcher/ipcp_isca2020.l1d_pref + .l2c_pref) instead of Pythia
               "ucp_ipcp": ("ucp", "ipcp"),
               "ipcp": (None, "ipcp"),
               "eq_ipcp": ("ucp", "ipcp")}
EQUAL_PRESETS = {"eq": "ucp", "eq_pythia": "ucp_pythia", "eq_ipcp": "ucp_ipcp"}
PREFETCHER_FILES = {"pythia": ("multi", "pythia"), "none": ("multi", "pythia"), "custom": ("multi", "pythia"),
                    "ipcp": ("ipcp_isca2020", "ipcp_isca2020")}      # (L1D, L2C) prefetcher files of the binary
DEGREE_ENV = {"pythia": "PYTHIA_DEGREE_CAPS", "ipcp": "IPCP_DEGREE_CAPS"}     # preset -> the adaptive-UCP preset it freezes
PYTHIA_ALONE_DEFAULT_REPL = "srrip"                          # LLC policy of 'pythia' / 'plain' unless --repl says otherwise
ALL_EXPERIMENTS = ("ucp", "ucp_pythia", "pythia")            # what  --experiment all  runs, in this order
GRID_EXPERIMENTS = ("ucp", "ucp_pythia", "pythia", "plain", "eq", "eq_pythia")
DEGREE_BASES = ("ucp_pythia", "eq_pythia", "pythia")         # what  --experiment degrees  sweeps
DEFAULT_DEGREES = "1,2,4"
FREEZE_INTERVAL = str(1 << 62)                               # UCP_PARTITION_INTERVAL value that never repartitions
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
            out += list(GRID_EXPERIMENTS) + sweep
        else:
            out.append(item)
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def make_experiment(args, spec, ap, multi):
    """Experiment settings (repl, prefetch mode, label, knobs, environment) of one experiment spec.  spec = None is the old
    spelling without --experiment (--repl / --mode / --label)."""
    a = copy.copy(args)
    a.ways = a.deg = None
    a.equal = False
    a.bw = None                  # +bw=MTPS: DRAM transfer rate (runtime knob --dram_io_freq of the simulator)
    a.umon_demand = False        # +umon=demand: UCP's utility monitor ignores prefetch fills (needs the -knob binary)
    a.extra_opts = list(args.extra_opts)
    name, knobs = None, []
    if spec:
        name, *knobs = spec.split("+")
        if name not in EXPERIMENTS:
            ap.error("unknown experiment '%s' (presets: %s; groups: %s)" % (name, ", ".join(sorted(EXPERIMENTS)), ", ".join(GROUPS)))
    for k in knobs:
        key, _, val = k.partition("=")
        if key == "ways":
            a.ways = parse_vector(val, "+ways", ap, allow_equal=True, minimum=1)
        elif key in ("deg", "degree"):
            a.deg = parse_vector(val, "+deg", ap, minimum=0)
        elif key == "bw":
            if not val.isdigit() or int(val) < 100:
                ap.error("+bw needs the DRAM transfer rate in MT/s, e.g. +bw=1200 (default 2400)")
            a.bw = int(val)
            a.extra_opts.append("--dram_io_freq=%d" % a.bw)
        elif key == "umon":
            if val != "demand":
                ap.error("+umon accepts only 'demand' (UCP utility monitor ignores prefetch accesses)")
            a.umon_demand = True
        else:
            ap.error("unknown knob '%s' in '%s' (use +ways=A-B-C-D or +deg=N)" % (k, spec))
    explicit = bool(knobs)
    if a.ways == "equal":                       # 'ucp+ways=equal' is the preset 'eq'
        if name not in EQUAL_PRESETS.values():
            ap.error("+ways=equal needs a UCP experiment (ucp / ucp_pythia), got '%s'" % spec)
        name = next(k for k, v in EQUAL_PRESETS.items() if v == name)
        a.ways = None
    if name in EQUAL_PRESETS:
        a.equal = True
        if a.ways is not None:
            ap.error("'%s' already uses the equal split: give either the preset or +ways" % spec)
    a.experiment, a.spec = name, spec
    # ---- LLC policy / prefetch mode
    if name:
        p_repl, p_mode = EXPERIMENTS[name]
        if args.mode not in (None, p_mode):
            ap.error("--experiment %s implies --mode %s (got --mode %s)" % (name, p_mode, args.mode))
        a.mode = p_mode
        if p_repl is None:                      # plain LLC of the user's choice
            a.repl = args.repl or PYTHIA_ALONE_DEFAULT_REPL
            if a.repl == "ucp":
                ap.error("--experiment %s has NO UCP partitioning: use --repl srrip or lru (UCP experiments: ucp, "
                         "ucp_pythia, eq, eq_pythia)" % name)
        else:
            if args.repl not in (None, p_repl) and not multi:
                ap.error("--experiment %s implies --repl %s (got --repl %s)" % (name, p_repl, args.repl))
            a.repl = p_repl
    else:
        a.repl = args.repl or "ucp"
        a.mode = args.mode or "pythia"
    # ---- global --ways / --degree: applied where they make sense, an error if the user gave a knob explicitly
    if a.ways is None and not a.equal and args.ways_all:
        if a.repl == "ucp":
            a.ways = parse_vector(args.ways_all, "--ways", ap, allow_equal=False, minimum=1)
    if a.deg is None and args.degree_all is not None:
        if a.mode in DEGREE_ENV:
            a.deg = parse_vector(args.degree_all, "--degree", ap, minimum=0)
    if a.ways is not None:
        if a.repl != "ucp":
            ap.error("+ways needs a UCP experiment (the LLC policy of '%s' is %s)" % (spec or "this run", a.repl))
        if sum(a.ways) != args.llc_ways:
            ap.error("+ways=%s adds up to %d, the LLC has %d ways" % (vec_text(a.ways), sum(a.ways), args.llc_ways))
    if a.deg is not None and a.mode not in DEGREE_ENV:
        ap.error("+deg needs a Pythia / IPCP experiment ('%s' has no prefetcher)" % (spec or "this run"))
    # ---- label = results sub-folder
    if args.label and not multi:
        a.label = args.label
    else:
        if name in EQUAL_PRESETS:
            base = name
        elif a.mode == "none":
            base = a.repl                                # e.g. ucp, srrip
        elif a.repl == "ucp":
            base = "ucp_%s" % a.mode                     # e.g. ucp_pythia
        else:
            base = "%s_%s" % (a.mode, a.repl)            # e.g. pythia_srrip, pythia_lru
        a.label = base + ("_w%s" % ways_str(a.ways) if a.ways else "") + ("_d%s" % vec_text(a.deg) if a.deg else "") \
            + ("_umondemand" if a.umon_demand else "") + ("_bw%d" % a.bw if a.bw else "")
    # ---- environment of the simulations
    a.env = {}
    if a.equal:
        a.env["UCP_PARTITION_INTERVAL"] = FREEZE_INTERVAL
    if a.ways:
        a.env["UCP_FIXED_ALLOCATION"] = ",".join(str(x) for x in a.ways)
    if a.deg is not None:
        a.env[DEGREE_ENV[a.mode]] = ",".join(str(x) for x in a.deg)
    a.needs_knob = bool(a.ways) or a.deg is not None or a.umon_demand
    if a.umon_demand:
        if a.repl != "ucp":
            ap.error("+umon=demand needs a UCP experiment")
        a.env["UCP_DEMAND_ONLY_UMON"] = "1"
    a.ways_text = ("equal" if a.equal else ways_str(a.ways) if a.ways else ("adaptive" if a.repl == "ucp" else "none"))
    a.deg_text = vec_text(a.deg) if a.deg is not None else "original"
    a.knob_text = " ".join("%s=%s" % kv for kv in sorted(a.env.items()))
    return a


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


def load_mixes_csv(path):
    """Mixes from a CSV written by write_mixes_csv (any core count = --cores); ids must be 1..N."""
    mixes = []
    with open(path) as f:
        for r in csv.DictReader(f):
            mixes.append(Mix(int(r["mix_id"]), r["mix_name"], r["type"],
                             tuple(r["core%d_class" % c] for c in range(NCORES)),
                             tuple(r["core%d_trace" % c] for c in range(NCORES))))
    mixes.sort(key=lambda m: m.id)
    if [m.id for m in mixes] != list(range(1, len(mixes) + 1)):
        sys.exit("[ERROR] %s: mix ids must be 1..N" % path)
    return mixes


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
    """The knob binary (built only when an experiment uses +ways / +deg) has the suffix -knob; everything else runs on the
    original binary of its LLC policy."""
    l1d, l2c = PREFETCHER_FILES[getattr(args, "mode", None) or "pythia"]
    return "%s-%s-%s-no-%s-%dcore-llc%ds%dw%s" % (args.bp, l1d, l2c, args.repl, NCORES, args.llc_sets, args.llc_ways,
                                                  "-knob" if getattr(args, "needs_knob", False) else "")


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


LLC_OCC_CODE = r"""
// ===== LLC occupancy statistics -- read-only, inserted by run_4core_mixes.py --build; does not change the simulation
#include <cstdio>
#define LLC_OCC_INTERVAL @INTERVAL@ULL
static uint64_t occ_next_cycle = 0, occ_samples = 0, occ_sum_valid = 0;
static uint64_t occ_sum_core[NUM_CPUS], occ_sum_pf[NUM_CPUS];

// count the valid LLC blocks owned by every core (owner = core that filled the block) and, of those, the blocks
// that were brought in by a prefetch and not used yet (the 'prefetch' flag is cleared on the first demand hit)
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
// ===== end of LLC occupancy statistics

"""


def patch_llc_occupancy(tmp, interval):
    """Add read-only LLC occupancy sampling to src/main.cc of the throw-away build copy.  From the end of the warm-up
    until the last core finishes, the LLC is scanned every <interval> cycles; the average number of blocks owned by each
    core (and how many of them are unused prefetches) is printed at the end of the log as Core_<n>_LLC_occupancy_* lines."""
    f = tmp / "src/main.cc"
    txt = f.read_text()
    edits = [("int main(int argc, char** argv)", LLC_OCC_CODE.replace("@INTERVAL@", str(int(interval))) + "int main(int argc, char** argv)", True),
             ("        uncore.cycle++;\n",
              "        uncore.cycle++;\n        if (all_warmup_complete > NUM_CPUS && uncore.cycle >= occ_next_cycle) { llc_occ_sample(); "
              "occ_next_cycle = uncore.cycle + LLC_OCC_INTERVAL; }\n", False),
             ("\n    uncore.LLC.llc_prefetcher_final_stats();\n", "\n    uncore.LLC.llc_prefetcher_final_stats();\n    print_llc_occupancy();\n", False)]
    for old, new, before in edits:
        if txt.count(old) != 1:
            sys.exit("[ERROR] cannot add the LLC-occupancy statistics: expected exactly one '%s' in src/main.cc (found %d). "
                     "Use --no-occupancy to build without them." % (old.strip(), txt.count(old)))
        txt = txt.replace(old, new)
    f.write_text(txt)
    print("[BUILD] added LLC-occupancy statistics (sampled every %d cycles after warm-up)" % interval)



# --------------------------------------------------------------------------
# Experiment knobs (only compiled into the "-knob" binary, always in the throw-away copy of the sources)
# --------------------------------------------------------------------------
UCP_FIXED_CODE = r"""
    // ===== experiment knob (added by run_4core_mixes.py --build): UCP_FIXED_ALLOCATION="a,b,c,d" pins the LLC way allocation
    // (one entry per core, sum = LLC_WAY, each >= 1) and switches the periodic repartitioning off.  Unset = original UCP.
    if (const char *fx = std::getenv("UCP_FIXED_ALLOCATION")) {
        if (*fx) {
            std::vector<uint32_t> parsed;
            const char *p = fx;
            while (*p) {
                char *end = nullptr;
                const unsigned long v = std::strtoul(p, &end, 10);
                if (end == p || v == 0 || v > LLC_WAY || (*end != ',' && *end != '\0')) {
                    std::fprintf(stderr, "[UCP ERROR] bad UCP_FIXED_ALLOCATION='%s'\n", fx);
                    std::exit(1);
                }
                parsed.push_back(static_cast<uint32_t>(v));
                p = (*end == ',') ? end + 1 : end;
            }
            uint32_t sum = 0;
            for (uint32_t v : parsed)
                sum += v;
            if (parsed.size() != NUM_CPUS || sum != LLC_WAY) {
                std::fprintf(stderr, "[UCP ERROR] UCP_FIXED_ALLOCATION='%s' must have %u entries adding up to %u\n", fx,
                             (unsigned)NUM_CPUS, (unsigned)LLC_WAY);
                std::exit(1);
            }
            for (uint32_t c = 0; c < NUM_CPUS; ++c)
                quota[c] = parsed[c];
            fixed_partition_mode = true;
        }
    }
    // ===== end of experiment knob
"""

UCP_FIXED_ANNOUNCE = r"""    if (fixed_partition_mode) {
        partition_interval = static_cast<uint64_t>(1) << 62;   // never repartition
        std::printf("[UCP] Fixed LLC way allocation active:");
        for (uint32_t c = 0; c < NUM_CPUS; ++c)
            std::printf(" CPU%u=%u", c, quota[c]);
        std::printf(" (total=%u)\n", (unsigned)LLC_WAY);
    }
"""

PYTHIA_CAP_CODE = r"""
// ===== experiment knob (added by run_4core_mixes.py --build): PYTHIA_DEGREE_CAPS="d0,d1,d2,d3" = one upper bound of the
// Pythia prefetch degree per core (one value = all cores).  Pythia's own adaptive degree is 1..6; 0 switches the L2C
// prefetcher of that core off.  Unset = original behaviour.
#include <cstdlib>
#include <sstream>
#include <vector>
static int pythia_degree_cap(uint32_t cpu)
{
	static const std::vector<int> caps = []() {
		std::vector<int> v;
		if (const char *e = std::getenv("PYTHIA_DEGREE_CAPS")) {
			std::stringstream ss(e);
			std::string tok;
			while (std::getline(ss, tok, ','))
				v.push_back(std::atoi(tok.c_str()));
		}
		if (v.empty())
			v.push_back(-1);
		if (v.size() == 1)
			v.resize(NUM_CPUS, v[0]);
		while (v.size() < NUM_CPUS)
			v.push_back(v.back());
		return v;
	}();
	return caps[cpu < caps.size() ? cpu : caps.size() - 1];
}
// ===== end of experiment knob
"""


def patch_knobs(tmp, args):
    """Add the experiment knobs to the throw-away build copy: UCP_FIXED_ALLOCATION (UCP only) and PYTHIA_DEGREE_CAPS.
    Every edit must match exactly once, so a changed source tree is reported instead of silently built wrong."""
    def edit(rel, edits):
        f = tmp / rel
        txt = f.read_text()
        for old, new in edits:
            if txt.count(old) != 1:
                sys.exit("[ERROR] cannot add the experiment knobs: expected exactly one '%s' in %s (found %d). The sources differ "
                         "from the ones this script was written for." % (old.strip()[:70], rel, txt.count(old)))
            txt = txt.replace(old, new)
        f.write_text(txt)

    if args.repl == "ucp":
        edit("replacement/ucp.llc_repl", [
            ("    sample_access(cpu, full_addr, set);\n",
             "    {   // experiment knob UCP_DEMAND_ONLY_UMON=1: the utility monitor ignores prefetch fills\n"
             "        static const bool demand_only = [](){ const char *e = std::getenv(\"UCP_DEMAND_ONLY_UMON\"); return e && *e == '1'; }();\n"
             "        if (!(demand_only && type == PREFETCH))\n"
             "            sample_access(cpu, full_addr, set);\n"
             "    }\n"),
            ("bool started_roi_schedule = false;\n", "bool started_roi_schedule = false;\nbool fixed_partition_mode = false;   // experiment knob UCP_FIXED_ALLOCATION\n"),
            ("    for (uint32_t i = 0; i < LLC_WAY % NUM_CPUS; ++i)\n        ++quota[i];\n",
             "    for (uint32_t i = 0; i < LLC_WAY % NUM_CPUS; ++i)\n        ++quota[i];\n" + UCP_FIXED_CODE),
            ('    std::printf("[UCP] initialized:', UCP_FIXED_ANNOUNCE + '    std::printf("[UCP] initialized:')])
        print("[BUILD] added the UCP_FIXED_ALLOCATION knob (way allocation pinned when the variable is set)")
    edit("inc/scooby.h", [("class Scooby : public Prefetcher\n{\nprivate:\n",
                           "class Scooby : public Prefetcher\n{\npublic:\n\tint degree_cap = -1;   /* experiment knob (run_4core_mixes.py): upper bound "
                           "of the prefetch degree, -1 = original behaviour */\nprivate:\n")])
    edit("prefetcher/scooby.cc", [("\tassert(action_index < knob::scooby_max_actions);\n\n\tMYLOG(\"act_idx %u act %d\"",
                                   "\tif(degree_cap > 0 && pref_degree > (uint32_t)degree_cap)\n\t\tpref_degree = (uint32_t)degree_cap;   /* experiment knob */\n"
                                   "\tassert(action_index < knob::scooby_max_actions);\n\n\tMYLOG(\"act_idx %u act %d\"")])
    # the call of the Pythia (Scooby) prefetcher; its indentation differs between branches, so only the statement is matched
    INVOKE = "prefetchers[index]->invoke_prefetcher(ip, addr, cache_hit, type, pref_addr);\n"
    edit("prefetcher/pythia.l2c_pref", [
        ("using namespace std;\n", "using namespace std;\n" + PYTHIA_CAP_CODE),
        ("uint32_t CACHE::l2c_prefetcher_operate(uint64_t addr, uint64_t ip, uint8_t cache_hit, uint8_t type, uint32_t metadata_in)\n{\n\tvector<uint64_t> pref_addr;\n",
         "uint32_t CACHE::l2c_prefetcher_operate(uint64_t addr, uint64_t ip, uint8_t cache_hit, uint8_t type, uint32_t metadata_in)\n{\n"
         "\tconst int pf_cap = pythia_degree_cap(cpu);   /* experiment knob */\n\tif(pf_cap == 0)\n\t\treturn metadata_in;\n\tvector<uint64_t> pref_addr;\n"),
        (INVOKE, "if(pf_cap > 0 && !knob::l2c_prefetcher_types[index].compare(\"scooby\"))\n"
                 "\t\t\t((Scooby*)prefetchers[index])->degree_cap = pf_cap;   /* experiment knob */\n\t\t" + INVOKE)])
    print("[BUILD] added the PYTHIA_DEGREE_CAPS knob (Pythia degree clamped per core when the variable is set)")


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
        if not args.no_occupancy:
            patch_llc_occupancy(tmp, args.occupancy_interval)
        if args.needs_knob:
            patch_knobs(tmp, args)
        env = dict(os.environ, PYTHIA_LLC_REPLACEMENT=args.repl, PYTHIA_BRANCH_PRED=args.bp,
                   TERM=os.environ.get("TERM", "xterm"), MAKEFLAGS="-j%d" % args.build_jobs)
        log = root / RESULTS_ROOT / "_build" / ("build_%s.log" % binary_name(args))
        log.parent.mkdir(parents=True, exist_ok=True)
        print("[BUILD] %s -> compiling (log: %s) ..." % (binary_name(args), log))
        with open(log, "w") as lf:
            l1d, l2c = PREFETCHER_FILES[args.mode]
            rc = subprocess.call(["./build_champsim.sh", l1d, l2c, "no", str(NCORES)],
                                 cwd=str(tmp), env=env, stdout=lf, stderr=subprocess.STDOUT)
        built = tmp / "bin" / ("%s-%s-%s-no-%s-%dcore" % (args.bp, l1d, l2c, args.repl, NCORES))
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
    if args.mode in ("none", "ipcp"):      # IPCP binaries do not use the multi/Pythia prefetcher knobs
        return ["--config=%s" % (root / "config/nopref.ini")]
    return []   # custom: everything comes from --extra-opts


def build_cmd(binary, root, args, traces, warm, sim):
    return ([str(binary), "--warmup_instructions=%d" % warm, "--simulation_instructions=%d" % sim]
            + mode_args(root, args) + args.extra_opts + ["-traces"] + traces)


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
            lf.write("# run_4core_mixes.py: experiment=%s ways=%s degree=%s env=%s\n"
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
def build_hint(args, a):
    """Command-line flags that select the experiment(s) when telling the user how to build the missing binary."""
    if args.experiment:
        return "--experiment %s " % args.experiment
    return "--repl %s " % a.repl


def print_experiments(args):
    print("Presets (LLC policy, prefetcher):")
    notes = {"ucp": "UCP partitioning, no prefetcher", "ucp_pythia": "UCP + Pythia (L2C)",
             "pythia": "Pythia alone, plain LLC (SRRIP; --repl lru for LRU)", "plain": "plain LLC (SRRIP/--repl), no prefetcher",
             "eq": "UCP's way-owned LLC frozen at the equal split, no prefetcher", "eq_pythia": "equal split + Pythia"}
    for k in EXPERIMENTS:
        print("  %-11s %s" % (k, notes[k]))
    degs = [d for d in str(args.degrees).split(",") if d]
    print("Groups:\n  all         %s\n  grid        %s\n  degrees     %s\n  everything  grid + degrees"
          % (", ".join(ALL_EXPERIMENTS), ", ".join(GRID_EXPERIMENTS),
             ", ".join("%s+deg=%s" % (b, d) for b in DEGREE_BASES for d in degs)))
    print("Knobs:  +ways=A-B-C-D (UCP way split, sum %d) | +ways=equal | +deg=N or +deg=N0-N1-N2-N3 (Pythia degree bound, 0 = off)" % args.llc_ways)
    print("Examples:  --experiment grid | --experiment degrees | --experiment ucp_pythia+deg=2,ucp+ways=2-2-6-6")


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
                   "--mode none; ucp_pythia for UCP + Pythia; pythia_<repl> for Pythia on another policy, "
                   "e.g. ucp, ucp_pythia, pythia_srrip, pythia_lru)")
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
    b.add_argument("--experiment", default=None, metavar="SPEC[,SPEC...]",
                   help="preset(s): ucp = UCP, no prefetcher | ucp_pythia = UCP + Pythia | pythia = Pythia alone on a plain LLC "
                        "(SRRIP unless --repl says otherwise) | plain = plain LLC, no prefetcher | eq = UCP's LLC frozen at the equal "
                        "split, no prefetcher | eq_pythia = equal split + Pythia.  Groups: all (ucp, ucp_pythia, pythia), grid "
                        "(ucp, ucp_pythia, pythia, plain, eq, eq_pythia), degrees ({ucp_pythia, eq_pythia, pythia} x --degrees), "
                        "everything (grid + degrees).  A preset can carry knobs: ucp_pythia+deg=2, ucp+ways=2-2-6-6, "
                        "eq_pythia+deg=0-4-4-0.  Several experiments run in ONE command and share the --jobs threads "
                        "(--list-experiments shows everything)")
    b.add_argument("--degrees", default=DEFAULT_DEGREES, help="Pythia degree bounds swept by --experiment degrees / everything")
    b.add_argument("--ways", dest="ways_all", default=None, metavar="A-B-C-D",
                   help="freeze the UCP way allocation of every UCP experiment of this command at this split (same as +ways=)")
    b.add_argument("--degree", dest="degree_all", default=None, metavar="N[-N-N-N]",
                   help="bound the Pythia degree of every Pythia experiment of this command (same as +deg=)")
    b.add_argument("--list-experiments", action="store_true", help="print the presets, groups and knob syntax and exit")
    b.add_argument("--repl", default=None,
                   help="LLC replacement policy (replacement/<name>.llc_repl).  Default: ucp, or srrip for "
                        "--experiment pythia.  Use e.g. --repl lru for the Pythia-alone baseline policy LRU")
    b.add_argument("--bp", default="perceptron", help="branch predictor (branch/<name>.bpred)")
    b.add_argument("--build", action="store_true", help="(re)build the binary with the fixed LLC geometry first")
    b.add_argument("--build-only", action="store_true", help="build and exit")
    b.add_argument("--build-jobs", type=int, default=os.cpu_count() or 4, help="make -j for --build")
    b.add_argument("--include-untracked", action="store_true",
                   help="--build: also copy untracked source files (default: tracked files only, like a clean clone)")
    b.add_argument("--keep-build-dir", action="store_true", help="keep the temporary build directory")
    b.add_argument("--no-occupancy", action="store_true",
                   help="--build: do NOT add the LLC-occupancy statistics (by default the build adds read-only sampling "
                        "that prints the LLC blocks owned by each core; it does not change any simulated result)")
    b.add_argument("--occupancy-interval", type=int, default=10000,
                   help="--build: LLC occupancy is sampled every this many cycles after warm-up")
    b.add_argument("--mode", choices=["pythia", "ipcp", "none", "custom"], default=None,
                   help="pythia: --l2c_prefetcher_types=scooby + config/pythia.ini; none: config/nopref.ini; "
                        "custom: only --extra-opts.  Default: pythia (or what --experiment implies)")
    b.add_argument("--extra-opts", default="", help="extra simulator options, e.g. '--config=/path/x.ini'")
    b.add_argument("--no-verify", action="store_true", help="skip the LLC-geometry check (not recommended)")
    m = ap.add_argument_group("mixes")
    m.add_argument("--cores", type=int, default=4, help="cores of the simulated system (1, 4, 8, ...); != 4 needs --mixes-csv")
    m.add_argument("--mixes-csv", type=Path, default=None, help="read the mixes from this CSV (columns of --write-mixes) "
                   "instead of generating the 50 four-core mixes")
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
    global NCORES
    NCORES = args.cores
    if args.list_experiments:
        print_experiments(args)
        return
    if args.experiment:
        specs = expand_specs(args.experiment, args, ap)
        if len(specs) > 1:
            for opt, flag in (("binary", "--binary"), ("label", "--label"), ("results_dir", "--results-dir"), ("mode", "--mode")):
                if getattr(args, opt):
                    ap.error("%s cannot be combined with several experiments (every experiment has its own binary / folder / mode)" % flag)
            if args.repl == "ucp":
                ap.error("with several experiments, --repl is the LLC policy of the plain-LLC runs (pythia, plain): srrip (default) or lru")
        exp_args = [make_experiment(args, sp, ap, len(specs) > 1) for sp in specs]
        labels = [a.label for a in exp_args]
        dup = {x for x in labels if labels.count(x) > 1}
        if dup:
            ap.error("these experiments would write to the same results folder: %s" % ", ".join(sorted(dup)))
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
        if not args.mixes_csv and len({family(t.name) for t in pools[c]}) < NCORES:
            sys.exit("[ERROR] class %s has fewer than 4 distinct benchmark families; lower --margin" % c)
    if args.mixes_csv:
        mixes = load_mixes_csv(args.mixes_csv)
    else:
        if NCORES != 4:
            sys.exit("[ERROR] the built-in mix generator makes 4-core mixes; give --mixes-csv for --cores %d" % NCORES)
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

    # ---- binaries (ucp and ucp_pythia share one binary, so 'all' needs at most two)
    if args.build or args.build_only:
        if args.binary:
            sys.exit("[ERROR] --build creates its own binary name; do not combine with --binary")
        built = set()
        for a in exp_args:
            if binary_name(a) not in built:
                built.add(binary_name(a))
                build_binary(root, a)
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
                        build_hint(args, lacking[0])))

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
                sys.exit("[ERROR] could not read the LLC configuration from %s (is it a Pythia-based ChampSim "
                         "binary?). Use --no-verify to skip this check." % a.binary_path.name)
            print("[CHECK] %s: cores=%s  LLC=%s sets x %s ways (%s KB)  -> %s" % (
                a.binary_path.name, cfg["num_cpus"], cfg["llc_set"], cfg["llc_way"], cfg["llc_size"],
                "OK" if not bad else "MISMATCH"))
            if bad:
                sys.exit("[ERROR] this binary does not have the required geometry: %s\n"
                         "  Build the right one with:  python3 %s %s--build --build-only"
                         % ("; ".join(bad), Path(__file__).name, build_hint(args, a)))
        todo_a = [m for m in selected if args.force or not runner.is_done(m)]
        plan.append((runner, a, todo_a, len(selected) - len(todo_a)))

    total_todo = sum(len(t) for _, _, t, _ in plan)
    for runner, a, todo_a, skipped in plan:
        with open(a.results_path / "run_manifest.txt", "a") as mf:
            mf.write("==== %s on %s\ncommand : %s\nlabel   : %s\nbinary  : %s\nrepl    : %s\nmode    : %s %s\nways    : %s\ndegree  : %s\nenv     : %s\n"
                     "warmup  : %g M, sim: %g M per core\n"
                     "LLC     : %d sets x %d ways\njobs    : %d%s\nmix seed: %d, margin %.2f\nmixes   : %s\n\n" % (
                         datetime.now().isoformat(timespec="seconds"), socket.gethostname(), " ".join(sys.argv),
                         a.label, a.binary_path, a.repl, a.mode, " ".join(a.extra_opts), a.ways_text, a.deg_text, a.knob_text or "-",
                         args.warmup, args.sim, args.llc_sets,
                         args.llc_ways, args.jobs, " (shared with the other experiments of --experiment all)" if multi else "",
                         args.seed, args.margin, ",".join(str(m.id) for m in selected)))
        print("Experiment: %s  (LLC policy: %s, prefetch mode: %s, ways: %s, Pythia degree: %s)\nBinary : %s\nResults: %s\nMixes  : %d selected, %d already done, %d to run | "
              "jobs=%d%s | warm-up %gM + sim %gM per core | LLC %dx%d"
              % (a.label, a.repl, a.mode, a.ways_text, a.deg_text, a.binary_path, a.results_path, len(selected), skipped, len(todo_a), args.jobs,
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
        print("\nNext:  python3 %s --compare-setups %s\n       python3 scripts/make_ucp_vs_pythia_excel.py"
              % (Path(__file__).name, " ".join(str(a.results_path) for a in exp_args)))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
