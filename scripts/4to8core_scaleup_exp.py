#!/usr/bin/env python3
"""
4to8core_scaleup_exp.py -- from 4 to 8 cores: do prefetching and LLC partitioning conflict under DRAM bandwidth pressure?
=========================================================================================================================
8-core study on the coord-controller tree (UCP + Pythia / IPCP), modelled on the many-core, bandwidth-constrained setting
of CLIP (MICRO'23).  Per-core resources are kept equal to the 4-core studies (LLC 512 KB per core: 4096 sets x 16 ways =
4 MB shared, 16 ways as before), so the only changes are more cores per DRAM channel and less bandwidth:

    4-core runs so far : 4 cores, 2 channels DDR4-2400  -> 9.6 GB/s per core
    8-core 'normal'    : 8 cores, 2 channels DDR4-2400  -> 4.8 GB/s per core
    8-core 'low'       : 8 cores, 2 channels at 1200 MT/s -> 2.4 GB/s per core   (CLIP: 64 cores / 4-8 channels = 1.6-3.2)

The SAME 20 class-balanced 8-core mixes are used by every part (4 homogeneous, 4 x 4+4, 6 x 6+2, 4 x 4+2+2, 2 x 2+2+2+2);
the trace choice rotates through all clear traces of every class, so all 49 traces are used.
Full runs: 25M warm-up + 150M simulated instructions per core.  Nothing in the simulator sources is changed: every run
goes through scripts/run_4core_mixes_fixingKnob.py (throw-away build copies, knobs as environment variables / options).

PARTS (priority order; no time limit by default -- stop it any time, everything finished is reported; --hours N sets one)
  0  alone IPC   every trace of the mixes alone on a 1-core system with the same 4 MB LLC (1 channel DDR4-2400):
                 no prefetcher / Pythia / IPCP  -> weighted speedup, harmonic speedup, max slowdown, fairness
  1  2x2 factorial at LOW bandwidth, both prefetchers:
                 plain LRU no-PF | UCP no-PF | LRU + PF | UCP + PF              (6 runs per mix; the no-PF pair is shared)
  2  the same factorial at NORMAL bandwidth                                       -> what the bandwidth cut does
     + UCP whose utility monitor ignores prefetch fills (UCP_DEMAND_ONLY_UMON), low bandwidth, both prefetchers
  3  ORACLE at LOW bandwidth (8-core version of oracle_search.py), both prefetchers: per-core degree caps x LLC way splits
     searched greedily at a short length (5M+15M), the 5 best (3 highest throughput + 2 fairest without throughput loss)
     validated at the full length and compared with the independent run of part 1 (UCP + native prefetcher).
     Partition granularity: with 16 ways for 8 cores many cores own 1-2 ways, so the oracle moves ONE way at a time
     (--way-moves 1,2 adds 2-way moves); --llc 2048x32 keeps the 4 MB but gives 32 ways (4 per core, as at 4 cores).

WHERE THE WORKBOOK (8core_report.xlsx) ANSWERS THE QUESTIONS
  prefetcher -> partition : UCP ways per core without vs with the prefetcher, prefetch-blind monitor -> 'PF->partition'
  partition -> prefetcher : accuracy / issued / useful / late per core on LRU vs UCP                  -> 'Partition->PF'
  per core: no PF & no partition | PF only | partition only | both; interaction = both / (PF only x partition only)
                                                                                 -> 'Per core', 'Interaction'
  per mix: weighted / harmonic speedup, max slowdown, fairness, MPKI, DRAM reads, miss latency, DRAM busy -> 'Per mix'
  bandwidth -> 'Bandwidth';  by class -> 'By class';  prefetch-blind UCP -> 'Knobs'
  room for coordination: best fixed per-core configuration vs the independent run -> 'Oracle', 'Oracle runs'

ONE COMMAND (repository root):
    nohup python3 scripts/4to8core_scaleup_exp.py --jobs 100 > scaleup.log 2>&1 &
    python3 scripts/4to8core_scaleup_exp.py --dry-run          # plan + time estimate
    python3 scripts/4to8core_scaleup_exp.py --report-only      # rebuild the workbook from the logs (also while running)
Re-running resumes; the workbook is refreshed every 30 minutes while the study runs.
"""
import argparse
import csv
import heapq
import importlib.util
import itertools
import json
import math
import random
import re
import signal
import statistics as st
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

RUNNER = "scripts/run_4core_mixes_fixingKnob.py"
CORES, LLC_SETS, LLC_WAYS = 8, 4096, 16      # --llc SETSxWAYS changes the LLC (same for every part)
WAY_MOVES = (1,)                             # --way-moves
LOW_BW, NORMAL_BW = 1200, 2400
PFS = {"pythia": dict(ucp="ucp_pythia", lru="pythia", eq="eq_pythia"),
       "ipcp": dict(ucp="ucp_ipcp", lru="ipcp", eq="eq_ipcp")}
CLASSES = ["HCHP", "HCLP", "LCHP", "LCLP"]
NATIVE = 6                      # degree cap that leaves the prefetcher unchanged (Pythia 1..6; IPCP GS 6, CS 3, CPLX 3, NL 1)
PRINT_LOCK = threading.Lock()


# ----------------------------------------------------------------------------------------------------------------------
# Oracle configurations: (ways, deg); ways = "A" (UCP adaptive) or a tuple of 8 way counts; deg = 8 degree caps
# ----------------------------------------------------------------------------------------------------------------------
def vtxt(v):
    return "-".join(str(x) for x in v)


def cfg_tag(cfg):
    return "w%s_d%s" % ("ucp" if cfg[0] == "A" else vtxt(cfg[0]), vtxt(cfg[1]))


def cfg_spec(pf, cfg):
    w, d = cfg
    parts = [PFS[pf]["ucp"]]
    if w != "A":
        parts.append("ways=" + vtxt(w))
    if any(x != NATIVE for x in d):
        parts.append("deg=" + vtxt(d))
    return "+".join(parts)


def equal_split():
    q, r = divmod(LLC_WAYS, CORES)
    return tuple(q + (1 if i < r else 0) for i in range(CORES))


def knob_kind(cfg):
    w, d = cfg
    pf_changed, part_changed = any(x != NATIVE for x in d), w != "A"
    return ("both knobs" if pf_changed and part_changed else "prefetch only" if pf_changed
            else "partition only" if part_changed else "baseline")


def neighbours(cur, seed, levels, first_round):
    """Single changes of an 8-core configuration: one core's degree cap; ways pinned (UCP's split / equal) or back to
    adaptive; WAY_MOVES ways given to a core by the largest other holder, or taken from it for the smallest holder;
    in the first round also uniform jumps (every core at one degree level)."""
    w, d = cur
    n = CORES
    out = []
    for i in range(n):
        for lv in levels:
            if lv != d[i]:
                out.append((w, d[:i] + (lv,) + d[i + 1:]))
    if w == "A":
        out += [(seed, d), (equal_split(), d)]
        split = seed
    else:
        out.append(("A", d))
        split = w
    for mv in WAY_MOVES:                     # give core i mv ways from the largest other holder / mv of its ways to the smallest
        for i in range(n):
            j = max((k for k in range(n) if k != i), key=lambda k: (split[k], -k))
            if split[j] - mv >= 1:
                v = list(split); v[j] -= mv; v[i] += mv
                out.append((tuple(v), d))
            if split[i] - mv >= 1:
                j = min((k for k in range(n) if k != i), key=lambda k: (split[k], k))
                v = list(split); v[i] -= mv; v[j] += mv
                out.append((tuple(v), d))
    if first_round:
        for lv in levels[:-1]:
            out.append((w, (lv,) * n))
        for lv in levels:
            out.append((equal_split(), (lv,) * n))
    seen, res = {cur}, []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


def say(msg):
    with PRINT_LOCK:
        print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ----------------------------------------------------------------------------------------------------------------------
# Mixes
# ----------------------------------------------------------------------------------------------------------------------
def load_runner(root):
    spec = importlib.util.spec_from_file_location("runner", root / RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_mixes(R, seed):
    """20 class-balanced 8-core mixes: every class alone, in pairs, triples and all four; every class is the majority
    somewhere and the minority somewhere.  Traces are picked least-used first, so all clear traces are used."""
    pools, _ = R.clear_pools(R.embedded_traces(), R.MEDIAN_CACHE, R.MEDIAN_PREFETCH, 1.75)
    H1, H2, L1, L2 = CLASSES                                  # HCHP, HCLP, LCHP, LCLP
    plan = [("homogeneous 8", {c: 8}) for c in CLASSES]
    plan += [("two-class 4+4", {a: 4, b: 4}) for a, b in ((H1, H2), (H2, L1), (L1, L2), (H1, L2))]
    plan += [("two-class 6+2", {a: 6, b: 2}) for a, b in ((H2, H1), (H1, L1), (L1, H2), (L2, H1), (H2, L2), (H1, H2))]
    plan += [("three-class 4+2+2", {a: 4, b: 2, c: 2}) for a, b, c in ((H1, H2, L1), (H2, L1, L2), (L1, H1, L2),
                                                                         (L2, H2, L1))]
    plan += [("four-class 2+2+2+2", {c: 2 for c in CLASSES})] * 2
    rng = random.Random(seed)
    use = Counter()
    mixes = []
    for i, (mtype, comp) in enumerate(plan, 1):
        cores = []
        for c in CLASSES:
            k = comp.get(c, 0)
            if not k:
                continue
            cand = sorted(pools[c], key=lambda t: (use[t.name], rng.random()))
            fams, pick = set(), []
            for t in cand:                                   # distinct families first, then distinct traces
                if len(pick) < k and R.family(t.name) not in fams:
                    pick.append(t)
                    fams.add(R.family(t.name))
            for t in cand:
                if len(pick) < k and t not in pick:
                    pick.append(t)
            for t in pick:
                use[t.name] += 1
                cores.append((t.name, c))
        rng.shuffle(cores)
        label = "_".join("%dx%s" % (comp[c], c) for c in CLASSES if comp.get(c))
        mixes.append(dict(id=i, name="mix%02d_%s" % (i, label), type=mtype,
                          traces=[t for t, _ in cores], classes=[c for _, c in cores]))
    return mixes


def write_mixes(path, mixes, n):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mix_id", "mix_name", "type", "n_high_cache", "n_high_prefetch"]
                   + sum([["core%d_trace" % c, "core%d_class" % c] for c in range(n)], []))
        for m in mixes:
            w.writerow([m["id"], m["name"], m["type"], sum(c.startswith("HC") for c in m["classes"]),
                        sum(c.endswith("HP") for c in m["classes"])]
                       + sum([[t, c] for t, c in zip(m["traces"], m["classes"])], []))


# ----------------------------------------------------------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------------------------------------------------------
class Run:
    def __init__(self, tier, key, spec, lru, mix, cores, bw, cfg, pf, prio, group=None, length=None):
        self.tier, self.key, self.spec, self.lru, self.mix, self.cores = tier, key, spec, lru, mix, cores
        self.bw, self.cfg, self.pf, self.prio = bw, cfg, pf, prio
        self.group = group or "tier%d" % tier
        self.length = length                   # (warm-up, simulated) M instructions per core; None = the full length


def plan_runs(mixes, alone, a):
    runs = []
    low = "+bw=%d" % LOW_BW

    def add(tier, key, spec, lru, mix, cores, bw, cfg, pf, prio):
        runs.append(Run(tier, key, spec, lru, mix, cores, bw, cfg, pf, prio))
    for m in alone:                                                      # tier 0
        add(0, "alone_nopf", "plain", True, m["id"], 1, NORMAL_BW, "alone", None, (0, m["id"]))
        for p in PFS:
            add(0, "alone_" + p, p, True, m["id"], 1, NORMAL_BW, "alone", p, (0, m["id"]))
    for tier, ms, bwk, bw in ((1, mixes, low, LOW_BW), (2, mixes, "", NORMAL_BW)):
        sfx = "_bw%d" % bw
        for m in ms:
            pr = (tier, m["id"])
            add(tier, "lru_nopf" + sfx, "plain" + bwk, True, m["id"], CORES, bw, "none", None, pr)
            add(tier, "ucp_nopf" + sfx, "ucp" + bwk, False, m["id"], CORES, bw, "part", None, pr)
            for p, d in PFS.items():
                add(tier, "lru_%s%s" % (p, sfx), d["lru"] + bwk, True, m["id"], CORES, bw, "pf", p, pr)
                add(tier, "ucp_%s%s" % (p, sfx), d["ucp"] + bwk, False, m["id"], CORES, bw, "both", p, pr)
    sfx = "_bw%d" % LOW_BW
    for m in mixes:
        for p, d in PFS.items():
            add(2, "ucp_%s_umondemand%s" % (p, sfx), d["ucp"] + "+umon=demand" + low, False, m["id"], CORES, LOW_BW,
                "both_umon", p, (2, m["id"]))
    return runs


# ----------------------------------------------------------------------------------------------------------------------
# Logs
# ----------------------------------------------------------------------------------------------------------------------
_CACHE, _CL = {}, threading.Lock()


def parse_log(path, n):
    path = Path(path)
    k = (str(path), path.stat().st_mtime)
    with _CL:
        if k in _CACHE:
            return _CACHE[k]
    text = path.read_text(errors="replace")

    def g(rx, cast=float):
        m = re.search(rx, text, re.M | re.I)
        return cast(m.group(1)) if m else None
    cores = []
    for c in range(n):
        p = r"^Core_%d_" % c

        def gc(name):
            return g(p + name + r"\s+([0-9.eE+-]+)\s*$")
        cores.append(dict(
            ipc=gc("IPC"), ins=gc("instructions"), cyc=gc("cycles"),
            llc_load_miss=gc("LLC_load_miss"), llc_rfo_miss=gc("LLC_RFO_miss"), llc_miss=gc("LLC_total_miss"),
            llc_wb_miss=gc("LLC_writeback_miss"), llc_lat=gc("LLC_average_miss_latency"),
            l2_load_miss=gc("L2C_load_miss"), l1d_load_miss=gc("L1D_load_miss"),
            pf_issued=(gc("L1D_prefetch_issued") or 0) + (gc("L2C_prefetch_issued") or 0),
            pf_useful=(gc("L1D_prefetch_useful") or 0) + (gc("L2C_prefetch_useful") or 0),
            pf_useless=(gc("L1D_prefetch_useless") or 0) + (gc("L2C_prefetch_useless") or 0),
            pf_late=(gc("L1D_prefetch_late") or 0) + (gc("L2C_prefetch_late") or 0),
            occ=gc("LLC_occupancy_avg_blocks"), occ_pf=gc("LLC_occupancy_avg_prefetch_blocks")))
    quotas = [tuple(int(x) for x in re.findall(r"CPU\d+=(\d+)", ln))
              for ln in re.findall(r"^\[UCP\] partition #\d+.*quotas:.*$", text, re.M)]
    quotas = [q for q in quotas if len(q) == n]
    bw = [g(r"^DRAM_bw_level_%d\s+(\d+)" % k) for k in range(4)]
    rb_h = sum(float(x) for x in re.findall(r"^Channel_\d+_RQ_row_buffer_hit\s+(\d+)", text, re.M))
    rb_m = sum(float(x) for x in re.findall(r"^Channel_\d+_RQ_row_buffer_miss\s+(\d+)", text, re.M))
    cong = sum(float(x) for x in re.findall(r"^Channel_\d+_dbus_congested\s+(\d+)", text, re.M))
    res = dict(cores=cores, quotas=quotas, bw=bw if all(x is not None for x in bw) else None,
               rb_hit=rb_h / (rb_h + rb_m) if rb_h + rb_m else None, dbus_congested=cong,
               sim=g(r"^simulation_instructions\s+(\d+)", int), sets=g(r"^llc_set\s+(\d+)\s*$", int),
               ways=g(r"^llc_way\s+(\d+)\s*$", int), dram_freq=g(r"^dram_io_freq\s+(\d+)", int),
               complete=all(c["ipc"] for c in cores))
    with _CL:
        _CACHE[k] = res
    return res


def find_log(d):
    for f in sorted(Path(d).glob("mix*/mix*.txt")):
        if f.stem == f.parent.name:
            return f
    return None


# ----------------------------------------------------------------------------------------------------------------------
# Runner pool with priorities and a deadline
# ----------------------------------------------------------------------------------------------------------------------
class Job:
    def __init__(self, r):
        self.r, self.log, self.done = r, None, threading.Event()


class Study:
    def __init__(self, a, mixes, alone):
        self.a, self.mixes, self.alone = a, {m["id"]: m for m in mixes}, {m["id"]: m for m in alone}
        self.out = a.out
        self.heap, self.cv, self.seq = [], threading.Condition(), itertools.count()
        self.stop, self.procs, self.plock = False, set(), threading.Lock()
        self.walls = defaultdict(list)          # cores -> minutes per M instructions of finished runs
        self.status = {}                        # run dir -> OK / FAILED / SKIPPED
        self.jobs = {}                          # run dir -> Job (identical runs are simulated once)
        self.drivers = 0                        # oracle drivers still adding runs
        self.left = 0
        self.orows, self.opaths, self.olock = [], [], threading.Lock()
        self.deadline = float("inf")

    def length(self, r):
        return r.length or (self.a.warmup, self.a.sim)

    def run_dir(self, r):
        return self.out / r.group / r.key / ("mix%02d" % r.mix)

    def est_minutes(self, r):
        w = self.walls.get(r.cores)
        rate = st.median(w) if w and len(w) >= 5 else self.a.rate * r.cores / 4.0
        return rate * sum(self.length(r))

    def submit(self, r):
        d = str(self.run_dir(r))
        with self.cv:
            if d not in self.jobs:
                j = self.jobs[d] = Job(r)
                heapq.heappush(self.heap, (r.prio, next(self.seq), j))
                self.left += 1
                self.cv.notify()
            return self.jobs[d]

    def worker(self):
        while True:
            with self.cv:
                while not self.heap and not self.stop and self.drivers > 0:
                    self.cv.wait(5)
                if self.stop or not self.heap:
                    return
                _, _, j = heapq.heappop(self.heap)
            try:
                j.log = self.run(j.r)
            finally:
                j.done.set()
                with self.cv:
                    self.left -= 1
                    self.cv.notify_all()

    def run(self, r):
        d = self.run_dir(r)
        log = find_log(d)
        if log and parse_log(log, r.cores)["complete"]:
            self.status[str(d)] = "OK"
            return log
        if self.a.dry_run or self.stop:
            return None
        if r.tier > 0 and time.time() + self.est_minutes(r) * 60 > self.deadline:
            self.status[str(d)] = "SKIPPED (time budget)"
            return None
        d.mkdir(parents=True, exist_ok=True)
        root = self.a.root
        w, sm = self.length(r)
        csvp = self.out / ("mixes_alone.csv" if r.cores == 1 else "mixes_8core.csv")
        cmd = [sys.executable, str(root / RUNNER), "--root", str(root), "--cores", str(r.cores), "--mixes-csv", str(csvp),
               "--llc-sets", str(LLC_SETS), "--llc-ways", str(LLC_WAYS), "--experiment", r.spec, "--only", str(r.mix),
               "--warmup", "%g" % w, "--sim", "%g" % sm, "--results-dir", str(d), "--jobs", "1",
               "--no-verify"] + (["--repl", "lru"] if r.lru else []) + self.a.runner_args
        (d / "study_command.txt").write_text(" ".join(cmd) + "\n")
        t0 = time.time()
        with open(d / "runner_output.txt", "w") as f:
            p = subprocess.Popen(cmd, cwd=str(root), stdout=f, stderr=subprocess.STDOUT)
            with self.plock:
                self.procs.add(p)
            try:
                p.wait()
            finally:
                with self.plock:
                    self.procs.discard(p)
        if self.stop:
            return None
        wall = (time.time() - t0) / 60
        log = find_log(d)
        ok = log is not None and parse_log(log, r.cores)["complete"]
        self.status[str(d)] = "OK" if ok else "FAILED"
        if ok:
            self.walls[r.cores].append(wall / sum(self.length(r)))
        say("%-22s %-34s mix%02d %s %.0f min  (%d queued or running)" % (r.group, r.key, r.mix, "OK" if ok else
                                                                        "FAILED: %s" % d, wall, self.left - 1))
        return log if ok else None

    # ------------------------------------------------------------------------------------------------------------------
    # Oracle (part 3): greedy per-core search at a short length, best ones validated at the full length
    # ------------------------------------------------------------------------------------------------------------------
    def oracle_driver(self, pf, mix):
        try:
            self._oracle(pf, mix)
        except Exception as e:                  # one mix must never stop the study
            say("oracle %s mix%02d failed: %s" % (pf, mix, e))
        finally:
            with self.cv:
                self.drivers -= 1
                self.cv.notify_all()

    def _oracle(self, pf, mix):
        n, nat = CORES, (NATIVE,) * CORES
        short = self.a.oracle_len
        low = "+bw=%d" % LOW_BW

        def srun(cfg, prio):
            return Run(3, cfg_tag(cfg), cfg_spec(pf, cfg) + low, False, mix, n, LOW_BW, "oracle", pf, prio,
                       group="oracle_%s_search" % pf, length=short)
        base_cfg = ("A", nat)
        jb = self.submit(srun(base_cfg, (3, 0, mix)))
        jb.done.wait()
        if not jb.log:
            return
        bl = parse_log(jb.log, n)
        qs = [q for q in bl["quotas"] if sum(q) == LLC_WAYS and min(q) >= 1]
        seed = Counter(qs).most_common(1)[0][0] if qs else equal_split()
        evaluated = {base_cfg: (1.0, 1.0)}
        rows = []
        cur, cur_s = base_cfg, 1.0
        path = [dict(prefetcher=pf, mix=mix, round=0, config=cfg_tag(base_cfg), NWS=1.0,
                     note="baseline (short); UCP's most frequent split %s" % vtxt(seed))]
        for rnd in range(1, self.a.oracle_rounds + 1):
            cands = [c for c in neighbours(cur, seed, self.a.levels, rnd == 1) if c not in evaluated]
            if not cands:
                break
            js = [(c, self.submit(srun(c, (3, 2 - (rnd > 1), mix)))) for c in cands]
            best = None
            for c, j in js:
                j.done.wait()
                if not j.log:
                    continue
                pl = parse_log(j.log, n)
                sp = [x["ipc"] / y["ipc"] for x, y in zip(pl["cores"], bl["cores"])]
                evaluated[c] = (sum(sp) / n, min(sp))
                rows.append(dict(prefetcher=pf, mix=mix, phase="search", round=rnd, config=cfg_tag(c),
                                 ways="UCP adaptive" if c[0] == "A" else vtxt(c[0]), degree=vtxt(c[1]),
                                 kind=knob_kind(c), NWS=round(sum(sp) / n, 4), worst_core=round(min(sp), 4),
                                 **{"speedup_c%d" % i: round(x, 4) for i, x in enumerate(sp)}))
                if best is None or evaluated[c][0] > evaluated[best][0]:
                    best = c
            if best is not None and evaluated[best][0] > cur_s + 0.002:
                cur, cur_s = best, evaluated[best][0]
                path.append(dict(prefetcher=pf, mix=mix, round=rnd, config=cfg_tag(cur), NWS=round(cur_s, 4),
                                 note="accepted (%d tried)" % len(cands)))
            else:
                path.append(dict(prefetcher=pf, mix=mix, round=rnd, config=cfg_tag(best) if best else "-",
                                 note="no improvement -> stop (%d tried)" % len(cands)))
                break
        others = sorted([c for c in evaluated if c != base_cfg], key=lambda c: -evaluated[c][0])
        pick = others[:3]                                                   # 3 highest throughput
        fair = sorted([c for c in others if c not in pick and evaluated[c][0] >= 1.0], key=lambda c: -evaluated[c][1])
        pick += fair[:2]                                                    # + 2 fairest that do not lose throughput
        pick += [c for c in others if c not in pick][:5 - len(pick)]
        # validation at the full length, against the independent run of part 1 (same mix, same bandwidth)
        base_full = self.submit(Run(1, "ucp_%s_bw%d" % (pf, LOW_BW), PFS[pf]["ucp"] + low, False, mix, n, LOW_BW,
                                    "both", pf, (1, mix)))
        fj = [(c, self.submit(Run(3, cfg_tag(c), cfg_spec(pf, c) + low, False, mix, n, LOW_BW, "oracle", pf,
                                  (3, 0, mix), group="oracle_%s_final" % pf))) for c in pick]
        base_full.done.wait()
        for c, j in fj:
            j.done.wait()
            if not (j.log and base_full.log):
                continue
            pl, b = parse_log(j.log, n), parse_log(base_full.log, n)
            sp = [x["ipc"] / y["ipc"] for x, y in zip(pl["cores"], b["cores"])]
            rows.append(dict(prefetcher=pf, mix=mix, phase="validated", round="", config=cfg_tag(c),
                             ways="UCP adaptive" if c[0] == "A" else vtxt(c[0]), degree=vtxt(c[1]), kind=knob_kind(c),
                             NWS=round(sum(sp) / n, 4), worst_core=round(min(sp), 4), search_NWS=round(evaluated[c][0], 4),
                             **{"speedup_c%d" % i: round(x, 4) for i, x in enumerate(sp)}))
        with self.olock:
            self.orows.extend(rows)
            self.opaths.extend(path)
        if not self.a.dry_run and not self.stop:
            say("oracle %s mix%02d done: best searched NWS %.3f (%s)" % (pf, mix, cur_s, cfg_tag(cur)))

    def kill(self):
        with self.cv:
            self.stop = True
            self.heap.clear()
            self.cv.notify_all()
        with self.plock:
            for p in list(self.procs):
                try:
                    p.send_signal(signal.SIGTERM)
                except Exception:
                    pass

    # ------------------------------------------------------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------------------------------------------------------
    def collect(self, runs):
        L = {}
        for r in runs:
            log = find_log(self.run_dir(r))
            if log:
                pl = parse_log(log, r.cores)
                if pl["complete"]:
                    L[(r.key, r.mix)] = (r, pl)        # factorial / knob / alone runs only (no oracle)
        return L

    def analyse(self, runs):
        L = self.collect(runs)
        alone = {}                                  # (trace, pf or None) -> IPC
        for (key, mid), (r, pl) in L.items():
            if r.tier == 0:
                alone[(self.alone[mid]["traces"][0], r.pf)] = pl["cores"][0]["ipc"]
        A = defaultdict(list)
        geo_warn = set()
        for (key, mid), (r, pl) in L.items():
            if r.tier == 0:
                continue
            if (pl["sets"], pl["ways"]) != (LLC_SETS, LLC_WAYS):
                geo_warn.add(key)
            m = self.mixes[mid]
            bwsfx = "_bw%d" % r.bw
            ref = L.get(("lru_nopf" + bwsfx, mid))
            refc = ref[1]["cores"] if ref else None
            sp, sl = [], []
            for i, c in enumerate(pl["cores"]):
                ki = (c["ins"] or 1) / 1000.0
                al = alone.get((m["traces"][i], None))
                s = c["ipc"] / refc[i]["ipc"] if refc else None
                sp.append(s)
                sl.append(al / c["ipc"] if al else None)
                u, x = c["pf_useful"], c["pf_useless"]
                q = [qq[i] for qq in pl["quotas"]]
                A["core"].append(dict(
                    tier=r.tier, bw=r.bw, config=r.cfg, prefetcher=r.pf or "-", run=key, mix=mid, mix_type=m["type"],
                    core=i, trace=m["traces"][i], cls=m["classes"][i], ipc=round(c["ipc"], 5),
                    speedup_vs_noPF_noPart=round(s, 4) if s else None, slowdown_vs_alone=round(sl[-1], 4) if sl[-1] else None,
                    LLC_demand_MPKI=round(((c["llc_load_miss"] or 0) + (c["llc_rfo_miss"] or 0)) / ki, 3),
                    DRAM_reads_PKI=round(((c["llc_miss"] or 0) - (c["llc_wb_miss"] or 0)) / ki, 3),
                    L2_load_MPKI=round((c["l2_load_miss"] or 0) / ki, 3),
                    LLC_miss_latency=c["llc_lat"], pf_issued_PKI=round(c["pf_issued"] / ki, 3),
                    pf_useful_PKI=round(u / ki, 3), pf_accuracy=round(u / (u + x), 4) if u + x else None,
                    pf_late_share=round(c["pf_late"] / u, 4) if u else None,
                    LLC_blocks=c["occ"], LLC_unused_pf_share=round(c["occ_pf"] / c["occ"], 4) if c["occ"] else None,
                    ucp_mean_ways=round(sum(q) / len(q), 2) if q else None))
            ipcs = [c["ipc"] for c in pl["cores"]]
            ok_sl = all(x for x in sl)
            ws = sum(1 / x for x in sl) if ok_sl else None
            ins = sum(c["ins"] or 0 for c in pl["cores"]) / 1000.0
            bwl = pl["bw"]
            A["mix"].append(dict(
                tier=r.tier, bw=r.bw, config=r.cfg, prefetcher=r.pf or "-", run=key, mix=mid, mix_type=m["type"],
                sum_ipc=round(sum(ipcs), 4),
                NWS_vs_noPF_noPart=round(sum(sp) / len(sp), 4) if refc else None,
                worst_core_vs_noPF_noPart=round(min(sp), 4) if refc else None,
                casualties=sum(x < 0.98 for x in sp) if refc else None,
                weighted_speedup=round(ws, 4) if ws else None,
                harmonic_speedup=round(CORES / sum(sl), 4) if ok_sl else None,
                max_slowdown=round(max(sl), 4) if ok_sl else None,
                fairness_min_over_max=round(min(sl) / max(sl), 4) if ok_sl else None,
                LLC_demand_MPKI=round(sum((c["llc_load_miss"] or 0) + (c["llc_rfo_miss"] or 0) for c in pl["cores"]) / ins, 3),
                DRAM_reads_PKI=round(sum((c["llc_miss"] or 0) - (c["llc_wb_miss"] or 0) for c in pl["cores"]) / ins, 3),
                LLC_miss_latency=round(st.mean(c["llc_lat"] for c in pl["cores"] if c["llc_lat"]), 1),
                DRAM_busy_gt50=round((bwl[2] + bwl[3]) / sum(bwl), 4) if bwl and sum(bwl) else None,
                DRAM_busy_gt75=round(bwl[3] / sum(bwl), 4) if bwl and sum(bwl) else None,
                row_buffer_hit=round(pl["rb_hit"], 4) if pl["rb_hit"] is not None else None))
        # ---- interaction, per core and per mix: both / (PF only x partition only), all against plain LRU no-PF
        idx = {(d["run"], d["mix"], d["core"]): d for d in A["core"]}
        for d in A["core"]:
            if d["config"] != "both":
                continue
            sfx = "_bw%d" % d["bw"]
            p = d["prefetcher"]
            pf = idx.get(("lru_%s%s" % (p, sfx), d["mix"], d["core"]))
            pa = idx.get(("ucp_nopf" + sfx, d["mix"], d["core"]))
            if not (pf and pa and d["speedup_vs_noPF_noPart"]):
                continue
            prod = pf["speedup_vs_noPF_noPart"] * pa["speedup_vs_noPF_noPart"]
            A["inter"].append(dict(bw=d["bw"], prefetcher=p, mix=d["mix"], core=d["core"], trace=d["trace"], cls=d["cls"],
                                   prefetch_only=pf["speedup_vs_noPF_noPart"], partition_only=pa["speedup_vs_noPF_noPart"],
                                   both=d["speedup_vs_noPF_noPart"], product=round(prod, 4),
                                   interaction=round(d["speedup_vs_noPF_noPart"] / prod, 4),
                                   best_single=round(max(pf["speedup_vs_noPF_noPart"], pa["speedup_vs_noPF_noPart"]), 4),
                                   both_worse_than_best_single=d["speedup_vs_noPF_noPart"] <
                                   0.98 * max(pf["speedup_vs_noPF_noPart"], pa["speedup_vs_noPF_noPart"])))
        # ---- prefetcher -> partition: UCP ways with vs without the prefetcher (and with a prefetch-blind monitor)
        for d in A["core"]:
            if d["config"] not in ("both", "both_umon") or d["ucp_mean_ways"] is None:
                continue
            sfx = "_bw%d" % d["bw"]
            base = idx.get(("ucp_nopf" + sfx, d["mix"], d["core"]))
            pf = idx.get(("lru_%s%s" % (d["prefetcher"], sfx), d["mix"], d["core"]))
            if base and base["ucp_mean_ways"] is not None:
                A["pf2part"].append(dict(bw=d["bw"], prefetcher=d["prefetcher"], monitor="prefetch-blind" if
                                         d["config"] == "both_umon" else "UCP (sees prefetches)", mix=d["mix"],
                                         core=d["core"], trace=d["trace"], cls=d["cls"],
                                         ways_without_PF=base["ucp_mean_ways"], ways_with_PF=d["ucp_mean_ways"],
                                         delta_ways=round(d["ucp_mean_ways"] - base["ucp_mean_ways"], 2),
                                         pf_accuracy=d["pf_accuracy"], pf_issued_PKI=d["pf_issued_PKI"],
                                         prefetch_gain_on_LRU=pf["speedup_vs_noPF_noPart"] if pf else None))
        # ---- partition -> prefetcher: the same prefetcher on LRU vs UCP vs equal ways
        for d in A["core"]:
            if d["config"] != "pf":
                continue
            sfx = "_bw%d" % d["bw"]
            p = d["prefetcher"]
            u = idx.get(("ucp_%s%s" % (p, sfx), d["mix"], d["core"]))
            e = idx.get(("eq_%s%s" % (p, sfx), d["mix"], d["core"]))
            row = dict(bw=d["bw"], prefetcher=p, mix=d["mix"], core=d["core"], trace=d["trace"], cls=d["cls"])
            for nm, x in (("LRU", d), ("UCP", u), ("EQ", e)):
                for k in ("pf_accuracy", "pf_issued_PKI", "pf_useful_PKI", "pf_late_share", "LLC_unused_pf_share",
                          "LLC_demand_MPKI"):
                    row["%s_%s" % (k, nm)] = x[k] if x else None
            row["UCP_ways"] = u["ucp_mean_ways"] if u else None
            A["part2pf"].append(row)
        # ---- bandwidth: prefetcher gain at normal vs low bandwidth (mixes run at both)
        mixi = {(d["run"], d["mix"]): d for d in A["mix"]}
        for p in PFS:
            for mid in self.mixes:
                row = dict(prefetcher=p, mix=mid, mix_type=self.mixes[mid]["type"])
                okrow = True
                for bw in (NORMAL_BW, LOW_BW):
                    sfx = "_bw%d" % bw
                    for nm, a_, b_ in (("PF_gain_LRU", "lru_%s" % p, "lru_nopf"), ("PF_gain_UCP", "ucp_%s" % p, "ucp_nopf"),
                                       ("UCP_gain_noPF", "ucp_nopf", "lru_nopf"), ("UCP_gain_PF", "ucp_%s" % p, "lru_%s" % p)):
                        x, y = mixi.get((a_ + sfx, mid)), mixi.get((b_ + sfx, mid))
                        if x and y:
                            row["%s_%d" % (nm, bw)] = round(x["sum_ipc"] / y["sum_ipc"], 4)
                        else:
                            okrow = False
                    w = mixi.get(("ucp_%s%s" % (p, sfx), mid))
                    row["WS_both_%d" % bw] = w["weighted_speedup"] if w else None
                if okrow:
                    A["bw"].append(row)
        # ---- by class
        for (bw, cfg, p, cl), ds in sorted(_group(A["core"], lambda d: (d["bw"], d["config"], d["prefetcher"], d["cls"])).items()):
            sp = [d["speedup_vs_noPF_noPart"] for d in ds if d["speedup_vs_noPF_noPart"]]
            sl = [d["slowdown_vs_alone"] for d in ds if d["slowdown_vs_alone"]]
            A["cls"].append(dict(bw=bw, config=cfg, prefetcher=p, cls=cl, cores=len(ds),
                                 mean_speedup=round(st.mean(sp), 4) if sp else None,
                                 casualty_share=round(sum(x < 0.98 for x in sp) / len(sp), 3) if sp else None,
                                 mean_slowdown_vs_alone=round(st.mean(sl), 3) if sl else None,
                                 mean_LLC_MPKI=round(st.mean(d["LLC_demand_MPKI"] for d in ds), 3),
                                 mean_DRAM_RPKI=round(st.mean(d["DRAM_reads_PKI"] for d in ds), 3)))
        # ---- summary per (bw, config, prefetcher)
        for (bw, cfg, p), ds in sorted(_group(A["mix"], lambda d: (d["bw"], d["config"], d["prefetcher"])).items()):
            def mean(k):
                v = [d[k] for d in ds if d.get(k) is not None]
                return round(st.mean(v), 4) if v else None

            def gmean(k):
                v = [d[k] for d in ds if d.get(k)]
                return round(math.exp(st.mean(math.log(x) for x in v)), 4) if v else None
            A["sum"].append(dict(bw=bw, config=cfg, prefetcher=p, mixes=len(ds),
                                 geomean_NWS_vs_noPF_noPart=gmean("NWS_vs_noPF_noPart"),
                                 mean_weighted_speedup=mean("weighted_speedup"), mean_harmonic_speedup=mean("harmonic_speedup"),
                                 mean_max_slowdown=mean("max_slowdown"), mean_fairness=mean("fairness_min_over_max"),
                                 casualty_cores=sum(d["casualties"] or 0 for d in ds),
                                 mean_LLC_MPKI=mean("LLC_demand_MPKI"), mean_DRAM_RPKI=mean("DRAM_reads_PKI"),
                                 mean_miss_latency=mean("LLC_miss_latency"), mean_DRAM_busy_gt50=mean("DRAM_busy_gt50"),
                                 mean_row_buffer_hit=mean("row_buffer_hit")))
        # ---- headline numbers
        H = []
        for p in PFS:
            for bw in (LOW_BW, NORMAL_BW):
                X = [d for d in A["inter"] if d["prefetcher"] == p and d["bw"] == bw]
                if X:
                    H.append(["%s @%d MT/s: cores where PF+UCP is >2%% worse than the best of PF-only / UCP-only" % (p, bw),
                              "%d of %d" % (sum(d["both_worse_than_best_single"] for d in X), len(X))])
                    H.append(["%s @%d MT/s: median interaction both/(PF x UCP); share < 0.98" % (p, bw),
                              "%.3f; %.0f%%" % (st.median(d["interaction"] for d in X),
                                                100 * sum(d["interaction"] < 0.98 for d in X) / len(X))])
                for cfg, nm in (("pf", "PF only (LRU)"), ("both", "PF + UCP")):
                    M = [d for d in A["mix"] if d["prefetcher"] == p and d["bw"] == bw and d["config"] == cfg
                         and d["NWS_vs_noPF_noPart"]]
                    if M:
                        H.append(["%s @%d MT/s: %s vs no PF/no partition: geomean NWS; mixes that lose > 2%%" % (p, bw, nm),
                                  "%.3f; %d of %d" % (math.exp(st.mean(math.log(d["NWS_vs_noPF_noPart"]) for d in M)),
                                                      sum(d["NWS_vs_noPF_noPart"] < 0.98 for d in M), len(M))])
            Q = [d for d in A["pf2part"] if d["prefetcher"] == p and d["monitor"].startswith("UCP")]
            if Q:
                H.append(["%s: cores whose UCP ways change by >= 1 when the prefetcher is on" % p,
                          "%d of %d (mean |change| %.2f ways)" % (sum(abs(d["delta_ways"]) >= 1 for d in Q), len(Q),
                                                                 st.mean(abs(d["delta_ways"]) for d in Q))])
        A["head"] = H
        A["geo_warn"] = sorted(geo_warn)
        return A

    def write_report(self, runs):
        A = self.analyse(runs)
        self.out.mkdir(parents=True, exist_ok=True)
        stat = Counter(v.split(" ")[0] for v in self.status.values())
        with open(self.out / "8core_summary.json", "w") as f:
            json.dump(dict(headline=A["head"], summary=A["sum"], status=stat), f, indent=1)
        for name in ("core", "mix", "inter"):
            if A[name]:
                with open(self.out / ("8core_%s.csv" % name), "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=list(A[name][0].keys()))
                    w.writeheader()
                    w.writerows(A[name])
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font
        except ImportError:
            say("openpyxl missing: CSV files only")
            return
        bold = Font(bold=True)
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"
        ws.append(["8-core prefetching x LLC partitioning study (coord-controller tree): %d cores, LLC %d sets x %d ways, "
                   "%g+%gM instructions per core; low bandwidth = %d MT/s, normal = %d MT/s, 2 channels"
                   % (CORES, LLC_SETS, LLC_WAYS, self.a.warmup, self.a.sim, LOW_BW, NORMAL_BW)])
        ws["A1"].font = bold
        ws.append(["simulations: %s" % dict(stat)])
        if A["geo_warn"]:
            ws.append(["WARNING: LLC geometry differs in: %s" % ", ".join(A["geo_warn"])])
        ws.append([])
        ws.append(["Headline numbers"])
        ws[ws.max_row][0].font = bold
        for h in A["head"]:
            ws.append(h)
        ws.append([])
        _table(ws, A["sum"], bold)
        for t in ("config: none = plain LRU no PF | part = UCP no PF | pf = PF on LRU | both = PF + UCP | eq_* = equal ways "
                  "| *_d2 = PF degree cap 2 | both_umon = UCP utility monitor ignores prefetch fills",
                  "NWS = mean per-core IPC / IPC on plain LRU without PF (same mix, same bandwidth); weighted / harmonic "
                  "speedup and slowdowns use the IPC of the trace alone on a 1-core system without PF",
                  "casualty = core > 2% slower than on plain LRU without PF"):
            ws.append([t])
        for name, data, note in (
                ("Per mix", A["mix"], "One row per simulation: throughput, fairness, misses, DRAM"),
                ("Per core", A["core"], "One row per core per simulation"),
                ("Interaction", A["inter"], "Per core, vs plain LRU no PF: PF only, UCP only, both; interaction = both / "
                 "(PF only x UCP only); < 1 = the two hurt each other; both_worse_than_best_single = running both is >2% "
                 "worse than the better of the two alone"),
                ("PF->partition", A["pf2part"], "UCP way allocation (mean over the run) per core without vs with the "
                 "prefetcher, and with a prefetch-blind utility monitor (knob tier)"),
                ("Partition->PF", A["part2pf"], "The same prefetcher on plain LRU vs UCP vs equal ways (EQ only in the knob "
                 "tier): accuracy, issued / useful per kilo-instruction, late share, unused-prefetch share of the LLC"),
                ("Bandwidth", A["bw"], "Mixes run at both bandwidths: gains (sum-IPC ratios) at normal vs low bandwidth"),
                ("By class", A["cls"], "Per-core results aggregated by class")):
            w2 = wb.create_sheet(name)
            w2.append([note])
            w2["A1"].font = bold
            if data:
                hdr = list(data[0].keys())
                w2.append(hdr)
                for c in w2[2]:
                    c.font = bold
                for d in data:
                    w2.append([d.get(k) for k in hdr])
                w2.freeze_panes = "A3"
        knob = [d for d in A["mix"] if d["bw"] == LOW_BW and d["config"] in ("both", "both_umon")]
        w3 = wb.create_sheet("Knobs")
        w3.append(["Low bandwidth: UCP + PF with the normal utility monitor vs a prefetch-blind one (UCP_DEMAND_ONLY_UMON)"])
        w3["A1"].font = bold
        if knob:
            hdr = ["mix", "prefetcher", "config", "run", "NWS_vs_noPF_noPart", "worst_core_vs_noPF_noPart", "weighted_speedup",
                   "max_slowdown", "DRAM_reads_PKI", "LLC_demand_MPKI", "LLC_miss_latency"]
            w3.append(hdr)
            for c in w3[2]:
                c.font = bold
            for d in sorted(knob, key=lambda d: (d["mix"], d["prefetcher"], d["config"])):
                w3.append([d.get(k) for k in hdr])
        self.oracle_sheets(wb, bold)
        wb.save(self.out / "8core_report.xlsx")
        self.plots(A)
        say("report: %s (%s)" % (self.out / "8core_report.xlsx", dict(stat)))

    def oracle_sheets(self, wb, bold):
        from openpyxl.styles import Font  # noqa
        rows = sorted(self.orows, key=lambda r: (r["prefetcher"], r["mix"], r["phase"] != "validated", -r["NWS"]))
        ws = wb.create_sheet("Oracle", 1)
        ws.append(["Room for coordination at LOW bandwidth: best fixed per-core configuration (degree caps x LLC ways) vs the "
                   "independent run UCP + native prefetcher (part 1). Searched at %g+%gM, validated at %g+%gM per core."
                   % (*self.a.oracle_len, self.a.warmup, self.a.sim)])
        ws["A1"].font = bold
        ws.append(["Success criterion (as in the 4-core study): best validated config >= +3% NWS in at least 1/3 of the "
                   "mixes, OR worst core >= +5% without losing NWS in at least 1/3 of the mixes"])
        hdr = ["prefetcher", "mixes", "mean best NWS", "max best NWS", "mixes >= +3%", "mixes worst core +5% (no loss)",
               "best search NWS: prefetch only (mean)", "partition only (mean)", "both knobs (mean)", "verdict"]
        ws.append([])
        ws.append(hdr)
        for c in ws[ws.max_row]:
            c.font = bold
        per = []
        for pf in PFS:
            V = [r for r in rows if r["prefetcher"] == pf and r["phase"] == "validated"]
            mixes = sorted({r["mix"] for r in V})
            if not mixes:
                continue
            best = {m: max((r for r in V if r["mix"] == m), key=lambda r: r["NWS"]) for m in mixes}
            fair = {m: max([r for r in V if r["mix"] == m and r["NWS"] >= 1.0] or [r for r in V if r["mix"] == m],
                           key=lambda r: r["worst_core"]) for m in mixes}
            Sr = [r for r in rows if r["prefetcher"] == pf and r["phase"] == "search"]
            kb = {}
            for k in ("prefetch only", "partition only", "both knobs"):
                v = [max([r["NWS"] for r in Sr if r["mix"] == m and r["kind"] == k] or [None] or [1.0]) or 1.0
                     for m in mixes]
                kb[k] = round(st.mean(v), 4) if v else None
            n_gain = sum(best[m]["NWS"] >= 1.03 for m in mixes)
            n_fair = sum(fair[m]["worst_core"] >= 1.05 and fair[m]["NWS"] >= 1.0 for m in mixes)
            ws.append([pf, len(mixes), round(st.mean(best[m]["NWS"] for m in mixes), 4),
                       max(best[m]["NWS"] for m in mixes), n_gain, n_fair, kb["prefetch only"], kb["partition only"],
                       kb["both knobs"], "ROOM FOR COORDINATION" if 3 * max(n_gain, n_fair) >= len(mixes)
                       else "no clear room (static)"])
            for m in mixes:
                per.append([pf, m, self.mixes[m]["type"], best[m]["config"], best[m]["kind"], best[m]["NWS"],
                            best[m]["worst_core"], best[m].get("search_NWS"), fair[m]["config"], fair[m]["NWS"],
                            fair[m]["worst_core"]])
        ws.append([])
        ws.append(["prefetcher", "mix", "type", "best validated config", "kind", "NWS", "worst core", "NWS at search length",
                   "fairest (no loss)", "NWS", "worst core"])
        for c in ws[ws.max_row]:
            c.font = bold
        for r in per:
            ws.append(r)
        ws.append([])
        ws.append(["config names: w<8 way counts or ucp = adaptive>_d<8 degree caps>; %d = native, 0 = prefetcher off" % NATIVE])
        w2 = wb.create_sheet("Oracle runs")
        if rows:
            keys = []
            for r in rows:
                for k in r:
                    if k not in keys:
                        keys.append(k)
            w2.append(keys)
            for c in w2[1]:
                c.font = bold
            for r in rows:
                w2.append([r.get(k) for k in keys])
            w2.freeze_panes = "A2"
        w3 = wb.create_sheet("Oracle search path")
        pk = ["prefetcher", "mix", "round", "config", "NWS", "note"]
        w3.append(pk)
        for p in sorted(self.opaths, key=lambda p: (p["prefetcher"], p["mix"], p["round"])):
            w3.append([p.get(k) for k in pk])

    def plots(self, A):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return
        fig, axs = plt.subplots(1, 3, figsize=(16, 4.2))
        cfgs = [("pf", "PF only"), ("part", "UCP only"), ("both", "PF + UCP")]
        for k, (p, ax) in enumerate(zip(PFS, axs[:2])):
            for j, (cfg, nm) in enumerate(cfgs):
                for b, bw in enumerate((NORMAL_BW, LOW_BW)):
                    v = sorted(d["speedup_vs_noPF_noPart"] for d in A["core"] if d["bw"] == bw and d["config"] == cfg
                               and (d["prefetcher"] in (p, "-")) and d["speedup_vs_noPF_noPart"])
                    if v:
                        ax.plot(range(len(v)), v, lw=1.2, ls="-" if bw == LOW_BW else ":",
                                c=("#2a7ab9", "#e0a030", "#2e8b57")[j], label="%s @%d" % (nm, bw))
            ax.axhline(1, c="k", lw=0.6)
            ax.set_title("%s: per-core speedup vs plain LRU without PF (sorted)" % p, fontsize=9)
            ax.set_xlabel("cores (sorted)", fontsize=8)
            ax.legend(fontsize=6)
        ax = axs[2]
        for p, col in zip(PFS, ("#2a7ab9", "#e0a030")):
            for bw, ls in ((LOW_BW, "-"), (NORMAL_BW, ":")):
                v = sorted(d["interaction"] for d in A["inter"] if d["prefetcher"] == p and d["bw"] == bw)
                if v:
                    ax.plot(range(len(v)), v, c=col, ls=ls, label="%s @%d" % (p, bw))
        ax.axhline(1, c="k", lw=0.6)
        ax.set_title("interaction both / (PF only x UCP only), per core (sorted)", fontsize=9)
        ax.legend(fontsize=6)
        fig.tight_layout()
        fig.savefig(self.out / "8core_percore.png", dpi=150)
        plt.close(fig)


def _group(rows, keyf):
    g = defaultdict(list)
    for r in rows:
        g[keyf(r)].append(r)
    return g


def _table(ws, data, bold):
    if not data:
        return
    hdr = list(data[0].keys())
    ws.append(hdr)
    for c in ws[ws.max_row]:
        c.font = bold
    for d in data:
        ws.append([d.get(k) for k in hdr])
    ws.append([])


def main():
    global LLC_SETS, LLC_WAYS, WAY_MOVES
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--root", type=Path, default=here.parent if here.name == "scripts" else Path.cwd())
    ap.add_argument("-j", "--jobs", type=int, default=100)
    ap.add_argument("--warmup", type=float, default=25, help="M instructions per core")
    ap.add_argument("--sim", type=float, default=150, help="M instructions per core")
    ap.add_argument("--hours", type=float, default=0, help="0 = no time limit; else no new simulation starts if it "
                    "would end after this many hours")
    ap.add_argument("--llc", default="%dx%d" % (LLC_SETS, LLC_WAYS), help="LLC SETSxWAYS for every 8-core and alone run")
    ap.add_argument("--way-moves", default="1", help="oracle: numbers of ways moved per step, e.g. 1 or 1,2")
    ap.add_argument("--rate", type=float, default=0.75, help="first guess: minutes per M instructions of a 4-core run "
                    "(scaled by cores/4); replaced by measured times")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--only-mixes", default="", help="test runs only: restrict to these 8-core mix ids, e.g. 1,5")
    ap.add_argument("--oracle-len", default="5+15", help="oracle search length W+S, M instructions per core")
    ap.add_argument("--oracle-rounds", type=int, default=1, help="greedy oracle rounds (1 = all single changes once)")
    ap.add_argument("--levels", default="0,2", help="oracle degree caps besides native (%d)" % NATIVE)
    ap.add_argument("--no-oracle", action="store_true", help="skip part 3")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--runner-arg", action="append", default=[], dest="runner_args")
    a = ap.parse_args()
    a.root = a.root.expanduser().resolve()
    a.out = (a.out or a.root / "scaleup_8core_results").resolve()
    w_, _, s_ = a.oracle_len.partition("+")
    a.oracle_len = (float(w_), float(s_))
    a.levels = tuple(sorted({int(x) for x in a.levels.split(",") if x} | {NATIVE}))
    LLC_SETS, LLC_WAYS = (int(x) for x in a.llc.lower().split("x"))
    if LLC_WAYS < CORES:
        ap.error("the LLC needs at least one way per core")
    WAY_MOVES = tuple(sorted({int(x) for x in a.way_moves.split(",") if x}))
    if not (a.root / RUNNER).is_file():
        ap.error("%s not found under %s" % (RUNNER, a.root))
    R = load_runner(a.root)
    mixes = make_mixes(R, a.seed)
    sel = {int(x) for x in a.only_mixes.split(",") if x}
    used = [m for m in mixes if not sel or m["id"] in sel]
    traces = sorted({t for m in used for t in m["traces"]})
    alone = [dict(id=i, name="mix%02d_alone_%s" % (i, t), type="alone", traces=[t], classes=["-"])
             for i, t in enumerate(traces, 1)]
    a.out.mkdir(parents=True, exist_ok=True)
    write_mixes(a.out / "mixes_8core.csv", mixes, CORES)
    write_mixes(a.out / "mixes_alone.csv", alone, 1)
    runs = [r for r in plan_runs(mixes, alone, a) if r.tier == 0 or not sel or r.mix in sel]
    omixes = [] if a.no_oracle else [m for m in used]
    cnt = Counter("part%d" % r.tier for r in runs)
    est = {k: sum(a.rate * (a.warmup + a.sim) * r.cores / 4.0 for r in runs if "part%d" % r.tier == k) for k in cnt}
    n1 = len(neighbours(("A", (NATIVE,) * CORES), (1, 1, 1, 1, 1, 1, 5, 5), a.levels, True))
    if omixes:
        cnt["part3 oracle"] = len(omixes) * len(PFS) * (1 + n1 * a.oracle_rounds + 5)
        est["part3 oracle"] = len(omixes) * len(PFS) * a.rate * CORES / 4.0 * (
            (1 + n1 * a.oracle_rounds) * sum(a.oracle_len) + 5 * (a.warmup + a.sim))
    print("8-core study: %d mixes (the same in every part), %d distinct traces; about %d simulations: %s"
          % (len(used), len(traces), sum(cnt.values()), ", ".join("%s %d" % kv for kv in sorted(cnt.items()))))
    print("  first estimate with %d jobs: %s  -> total %.1f h if perfectly packed (the real speed is measured as runs "
          "finish; --hours %g: 0 = no limit)"
          % (a.jobs, ", ".join("%s %.1f h" % (k, est[k] / a.jobs / 60) for k in sorted(est)),
             sum(est.values()) / a.jobs / 60, a.hours))
    print("  mix types: %s" % dict(Counter(m["type"] for m in mixes)))
    if a.dry_run:
        for m in mixes[:3]:
            print("  %s: %s" % (m["name"], " | ".join("%s[%s]" % x for x in zip(m["traces"], m["classes"]))))
        for r in [x for x in runs if x.mix == 1 and x.tier > 0]:
            print("  part%d mix01 %-30s --cores %d --experiment %s%s" % (r.tier, r.key, r.cores, r.spec,
                                                                        " --repl lru" if r.lru else ""))
        c = neighbours(("A", (NATIVE,) * CORES), (1, 1, 1, 1, 1, 1, 5, 5), a.levels, True)
        print("  part3 oracle: %d single changes per mix and prefetcher, e.g. --experiment %s+bw=%d" % (
            n1, cfg_spec("ipcp", c[-8]), LOW_BW))
        return
    S = Study(a, mixes, alone)
    def start_oracle():
        ds = []
        for p in PFS:
            for m in omixes:
                with S.cv:
                    S.drivers += 1
                t = threading.Thread(target=S.oracle_driver, args=(p, m["id"]), daemon=True)
                t.start()
                ds.append(t)
        return ds
    if a.report_only:
        a.dry_run = True
        for r in runs:
            S.submit(r)
        ds = start_oracle()
        ws_ = [threading.Thread(target=S.worker, daemon=True) for _ in range(8)]
        for t in ws_:
            t.start()
        for t in ds + ws_:
            t.join()
        S.write_report(runs)
        return
    if not a.no_build:
        specs8 = ["plain", "ucp"] + [x for d in PFS.values() for x in (d["ucp"], d["lru"])] + \
                 [d["ucp"] + "+deg=2" for d in PFS.values()]
        for cores, specs in ((CORES, specs8), (1, ["plain", "pythia", "ipcp"])):
            cmd = [sys.executable, str(a.root / RUNNER), "--root", str(a.root), "--cores", str(cores), "--mixes-csv",
                   str(a.out / ("mixes_8core.csv" if cores == CORES else "mixes_alone.csv")), "--llc-sets", str(LLC_SETS),
                   "--llc-ways", str(LLC_WAYS), "--experiment", ",".join(specs), "--repl", "lru", "--build",
                   "--build-only"] + a.runner_args
            say("building %d-core binaries" % cores)
            if subprocess.call(cmd, cwd=str(a.root)) != 0:
                sys.exit("[ERROR] build failed")
    S.deadline = time.time() + a.hours * 3600 if a.hours > 0 else float("inf")
    for r in runs:
        S.submit(r)
    start_oracle()
    threads = [threading.Thread(target=S.worker, daemon=True) for _ in range(a.jobs)]
    for t in threads:
        t.start()

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    say("started %d simulations with %d jobs; deadline %s" % (len(runs), a.jobs,
                                                            "none" if a.hours <= 0 else datetime.fromtimestamp(S.deadline).strftime("%a %H:%M")))
    last = time.time()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
            if time.time() - last > 1800:
                last = time.time()
                try:
                    S.write_report(runs)
                except Exception as e:          # a report problem must never stop the simulations
                    say("report failed: %s" % e)
    except KeyboardInterrupt:
        say("interrupted: stopping (finished runs are kept; re-run the same command to resume)")
        S.kill()
        time.sleep(3)
    S.write_report(runs)


if __name__ == "__main__":
    main()
