#!/usr/bin/env python3
"""
oracle_search.py -- is "UCP + prefetcher, each running on its own" the best combination?  A static oracle (limit) study.
=======================================================================================================================
For every selected 4-core mix it searches fixed per-core (prefetch degree cap x LLC ways) configurations and compares the
best ones with the independent baseline: UCP adaptive partitioning + the prefetcher's native, unmodified behaviour.
If a fixed configuration beats the baseline there is room for a coordination mechanism, and the winning configurations
show what that mechanism has to learn.  Both prefetchers are studied in the SAME tree (coord-controller branch: same
core, caches and DRAM), Pythia first; IPCP simulations only use the threads that Pythia leaves idle.

Nothing in the simulator sources is changed.  Every simulation is started through scripts/run_4core_mixes_fixingKnob.py,
which builds the binaries in a throw-away copy of the sources (LLC 2048 sets x 16 ways) and passes the knobs as
environment variables of the simulation:
    +ways=A-B-C-D   UCP way allocation pinned (UCP_FIXED_ALLOCATION; added to the throw-away copy only)
    +deg=a-b-c-d    per-core degree cap, 0 = prefetcher off (PYTHIA_DEGREE_CAPS: throw-away copy only;
                    IPCP_DEGREE_CAPS: already read by prefetcher/ipcp_isca2020.*)
Every run keeps the runner's usual folder (log, command.txt, status.txt).

ONE COMMAND (from the repository root):
    python3 scripts/oracle_search.py --jobs 100
    python3 scripts/oracle_search.py --dry-run          # plan + time estimate only
    python3 scripts/oracle_search.py --report-only      # rebuild the workbook / plots from the logs already there
Re-running the same command resumes: every simulation with a complete log is reused, nothing is simulated twice.

Per prefetcher and per mix
  1. SEARCH at a short length (default 5M warm-up + 25M simulated per core), greedy hill climbing on NWS:
       round 0  the baseline (UCP adaptive + native prefetcher)
       round 1  every single change of the baseline:
                  one core's degree cap -> another level (default 0, 1, 2, 4, native)
                  ways pinned at UCP's own most frequent split, ways pinned equal (4-4-4-4)
                  2 or 4 ways moved from one core to another (starting from UCP's split)
                  uniform jumps: all cores at one degree level (UCP adaptive ways and equal ways)
       round 2..R the same single changes around the best configuration so far; a step is kept only if it beats the
                current configuration by more than --min-gain; stops when nothing improves or the time budget is short
  2. VALIDATION at the reported length (default 25M warm-up + 100M simulated per core): the baseline, the 5 best searched
     configurations (3 best throughput + 2 fairest that do not lose throughput) and the independent reference points
     UCP without prefetching, the prefetcher on a plain LRU LLC, and a plain LRU LLC without prefetching (the two
     no-prefetcher references are the same for both prefetchers and are simulated once).

Search objective
  NWS = mean over the 4 cores of IPC / IPC in the baseline run (same mix, same length) = throughput.
Inferred from the same runs (no extra simulations), see the workbook tabs:
  worst-core speedup, harmonic speedup, casualties; DRAM reads/kilo-instr., demand LLC MPKI, prefetch accuracy, LLC miss
  latency, DRAM >50% busy; which knob holds the headroom (prefetch only / partition only / both -> coordination);
  self vs neighbour effect of each core's prefetcher; who gains / loses when ways move; prefetch x partitioning
  interaction per core; casualties of independent operation vs the best configuration; the winning policy per class.

Outputs in --out (default <root>/oracle_results):  oracle_report.xlsx, oracle_runs.csv, oracle_summary.json,
oracle_<prefetcher>.png and oracle_knobs.png; simulation folders under <prefetcher>/ and common/.
Only the Python standard library is required; openpyxl / matplotlib are used when installed.
"""
import argparse
import csv
import heapq
import itertools
import json
import math
import re
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

NCORES = 4
LLC_SETS = 2048                 # every binary (UCP, LRU, knob) is built with this LLC: 2048 sets x 16 ways = 2 MB shared
LLC_WAYS = 16
NATIVE = 6                      # degree cap that leaves the prefetcher unchanged (Pythia 1..6; IPCP GS 6, CS 3, CPLX 3, NL 1)
RUNNER = "scripts/run_4core_mixes_fixingKnob.py"
PFS = {
    "pythia": dict(base="ucp_pythia", lru_pf="pythia", name="UCP + Pythia"),
    "ipcp": dict(base="ucp_ipcp", lru_pf="ipcp", name="UCP + IPCP (L1D + L2C)"),
}
# reference points: (runner experiment, needs --repl lru, shared by all prefetchers)
CONTEXTS = {"ucp_nopf": ("ucp", False, True), "lru_pf": (None, True, False), "lru_nopf": ("plain", True, True)}
# 14 mixes: all 4 homogeneous classes, all 6 class pairs (2+2), two 3+1, one 2+1+1, one all-four.  8 of them had casualty
# cores in the earlier runs (03 09 12 16 24 28 33 49), 6 are controls (01 05 07 13 17 19).
DEFAULT_MIXES = "1,3,5,7,9,12,13,16,17,19,24,28,33,49"
RATE_MIN_PER_M = 0.5            # minutes of one 4-core simulation per million instructions per core (first estimate;
#                                 the script measures the real value as runs finish)

PRINT_LOCK = threading.Lock()


def say(msg):
    with PRINT_LOCK:
        print("%s %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


# ----------------------------------------------------------------------------------------------------------------------
# Configurations: (ways, deg); ways = "A" (UCP adaptive) or a tuple of 4 way counts; deg = tuple of 4 degree caps
# ----------------------------------------------------------------------------------------------------------------------
def vtxt(v):
    return "-".join(str(x) for x in v)


def cfg_tag(cfg):
    w, d = cfg
    return "w%s_d%s" % ("ucp" if w == "A" else vtxt(w), vtxt(d))


def cfg_spec(pf, cfg):
    w, d = cfg
    parts = [PFS[pf]["base"]]
    if w != "A":
        parts.append("ways=" + vtxt(w))
    if any(x != NATIVE for x in d):
        parts.append("deg=" + vtxt(d))
    return "+".join(parts)


def equal_split():
    q, r = divmod(LLC_WAYS, NCORES)
    return tuple(q + (1 if i < r else 0) for i in range(NCORES))


def neighbours(cur, seed, levels, moves, first_round):
    w, d = cur
    out = []
    for i in range(NCORES):                                     # one core's degree
        for lv in levels:
            if lv != d[i]:
                out.append((w, d[:i] + (lv,) + d[i + 1:]))
    if w == "A":                                                # ways: pin, or move ways from the pinned split
        out += [(seed, d), (equal_split(), d)]
        split = seed
    else:
        out.append(("A", d))
        split = w
    for k in moves:
        for i, j in itertools.permutations(range(NCORES), 2):
            if split[i] - k >= 1:
                s = list(split)
                s[i] -= k
                s[j] += k
                out.append((tuple(s), d))
    if first_round:                                             # uniform jumps a single-core step cannot reach
        for lv in levels[:-1]:
            out.append((w, (lv,) * NCORES))
        for lv in levels:
            out.append((equal_split(), (lv,) * NCORES))
    seen, res = {cur}, []
    for c in out:
        if c not in seen:
            seen.add(c)
            res.append(c)
    return res


# ----------------------------------------------------------------------------------------------------------------------
# Logs
# ----------------------------------------------------------------------------------------------------------------------
def grab(text, rx, cast=float):
    m = re.search(rx, text, re.M | re.I)
    return cast(m.group(1)) if m else None


_LOG_CACHE, _LOG_LOCK = {}, threading.Lock()


def parse_log(path):
    path = Path(path)
    key = (str(path), path.stat().st_mtime)
    with _LOG_LOCK:
        if key in _LOG_CACHE:
            return _LOG_CACHE[key]
    text = path.read_text(errors="replace")
    cores = []
    for c in range(NCORES):
        p = r"^Core_%d_" % c

        def g(name, cast=float):
            return grab(text, p + name + r"\s+([0-9.eE+-]+)\s*$", cast)
        cores.append(dict(ipc=g("IPC"), ins=g("instructions"), cyc=g("cycles"),
                          llc_miss=g("LLC_total_miss"), llc_wb_miss=g("LLC_writeback_miss"),
                          llc_load_miss=g("LLC_load_miss"), llc_rfo_miss=g("LLC_rfo_miss"),
                          llc_lat=g("LLC_average_miss_latency"),
                          pf_useful=sum(g(lv + "_prefetch_useful") or 0 for lv in ("L1D", "L2C")),
                          pf_useless=sum(g(lv + "_prefetch_useless") or 0 for lv in ("L1D", "L2C")),
                          occ=g("LLC_occupancy_avg_blocks"), occ_pf=g("LLC_occupancy_avg_prefetch_blocks")))
    quotas = [tuple(int(x) for x in re.findall(r"CPU\d=(\d+)", ln))
              for ln in re.findall(r"^\[UCP\] partition #\d+.*quotas:.*$", text, re.M)]
    fq = re.search(r"^\[UCP\] completed partitions=\d+ last quotas:(.*)$", text, re.M)
    fq = tuple(int(x) for x in re.findall(r"CPU\d=(\d+)", fq.group(1))) if fq else None
    bw = [grab(text, r"^DRAM_bw_level_%d\s+(\d+)" % k) for k in range(4)]
    res = dict(cores=cores, quotas=[q for q in quotas if len(q) == NCORES], final_quota=fq,
               bw=bw if all(x is not None for x in bw) else None,
               sim=grab(text, r"^simulation_instructions\s+(\d+)", int),
               llc_sets=grab(text, r"^llc_set\s+(\d+)\s*$", int),
               llc_ways=grab(text, r"^llc_way\s+(\d+)\s*$", int),
               complete=all(c["ipc"] for c in cores))
    with _LOG_LOCK:
        _LOG_CACHE[key] = res
    return res


def seed_split(log):
    """UCP's most frequent way split during the run (its final split if only that is printed; equal if neither)."""
    qs = [q for q in log["quotas"] if sum(q) == LLC_WAYS and min(q) >= 1]
    if qs:
        return Counter(qs).most_common(1)[0][0]
    q = log["final_quota"]
    if q and len(q) == NCORES and sum(q) == LLC_WAYS and min(q) >= 1:
        return q
    return equal_split()


def find_log(d, mix_id):
    for f in sorted(Path(d).glob("mix%02d_*/mix%02d_*.txt" % (mix_id, mix_id))):
        if f.stem == f.parent.name:
            return f
    return None


def metrics(log, base):
    out = {}
    cs, bs = log["cores"], base["cores"]
    sp = [c["ipc"] / b["ipc"] for c, b in zip(cs, bs)]
    out["speedup"], out["ipc"] = sp, [c["ipc"] for c in cs]
    out["NWS"] = sum(sp) / NCORES
    out["worst_core"] = min(sp)
    out["HS"] = NCORES / sum(1 / s for s in sp)
    out["casualties"] = sum(s < 0.98 for s in sp)
    out["sum_ipc_ratio"] = sum(out["ipc"]) / sum(b["ipc"] for b in bs)
    ins = sum(c["ins"] or 0 for c in cs)
    if ins:
        out["DRAM_RPKI"] = 1000.0 * sum((c["llc_miss"] or 0) - (c["llc_wb_miss"] or 0) for c in cs) / ins
        out["LLC_demand_MPKI"] = 1000.0 * sum((c["llc_load_miss"] or 0) + (c["llc_rfo_miss"] or 0) for c in cs) / ins
    u, x = sum(c["pf_useful"] for c in cs), sum(c["pf_useless"] for c in cs)
    out["pf_accuracy"] = u / (u + x) if u + x else None
    lat = [c["llc_lat"] for c in cs if c["llc_lat"]]
    out["LLC_miss_latency"] = sum(lat) / len(lat) if lat else None
    occ = [c["occ"] for c in cs if c["occ"] is not None]
    if occ and sum(occ):
        out["LLC_unused_pf_share"] = sum(c["occ_pf"] or 0 for c in cs) / sum(occ)
    if log["bw"]:
        tot = sum(log["bw"]) or 1
        out["DRAM_busy_gt50pct"] = (log["bw"][2] + log["bw"][3]) / tot
    return out


# ----------------------------------------------------------------------------------------------------------------------
# Simulation pool with priorities and a running estimate of the queued work
# ----------------------------------------------------------------------------------------------------------------------
class Pool:
    def __init__(self, n):
        self.n = n
        self.heap, self.cv, self.seq, self.stop = [], threading.Condition(), itertools.count(), False
        self.procs, self.plock = set(), threading.Lock()
        self.pending_m = 0.0                  # M instructions per core still queued or running
        self.done = []                        # (wall minutes, M instructions) of finished simulations
        for _ in range(n):
            threading.Thread(target=self.work, daemon=True).start()

    def submit(self, prio, weight_m, fn, *a):
        fu = Future()
        if self.stop:
            fu.set_result((None, None))
            return fu
        with self.cv:
            self.pending_m += weight_m
            heapq.heappush(self.heap, (prio, next(self.seq), weight_m, fn, a, fu))
            self.cv.notify()
        return fu

    def work(self):
        while True:
            with self.cv:
                while not self.heap and not self.stop:
                    self.cv.wait()
                if self.stop:
                    return
                _, _, w, fn, a, fu = heapq.heappop(self.heap)
            try:
                res = fn(self, *a)
                if res and res[1]:
                    with self.cv:
                        self.done.append((res[1], w))
                fu.set_result(res)
            except BaseException as e:      # noqa
                fu.set_exception(e)
            finally:
                with self.cv:
                    self.pending_m -= w

    def rate(self, default):
        if len(self.done) < 5:
            return default
        return statistics.median(wall / m for wall, m in self.done if m)

    def kill(self):
        with self.cv:
            self.stop = True
            for *_, fu in self.heap:
                if not fu.done():
                    fu.set_result((None, None))       # waiting mix drivers see "no log" and end quietly
            self.heap.clear()
            self.cv.notify_all()
        with self.plock:
            for p in list(self.procs):
                try:
                    p.send_signal(signal.SIGTERM)       # the runner stops its simulation and cleans up
                except Exception:
                    pass


# ----------------------------------------------------------------------------------------------------------------------
# The study
# ----------------------------------------------------------------------------------------------------------------------
class Study:
    def __init__(self, a):
        self.a = a
        self.out = a.out.resolve()
        self.rows, self.paths, self.rlock = [], [], threading.Lock()
        self.futs, self.flock = {}, threading.Lock()
        self.mixinfo = {}

    def lens(self, phase):
        return self.a.search_len if phase == "search" else self.a.final_len

    def run_dir(self, owner, phase, mix, key):
        w, s = self.lens(phase)
        return self.out / owner / ("%s_%g+%gM" % (phase, w, s)) / ("mix%02d" % mix) / key

    def run_one(self, pool, d, mix, phase, spec, lru):
        log = find_log(d, mix)
        if log and parse_log(log)["complete"]:
            return log, None
        if self.a.dry_run or pool.stop:
            return None, None
        d.mkdir(parents=True, exist_ok=True)
        w, s = self.lens(phase)
        root = self.a.root
        cmd = [sys.executable, str(root / RUNNER), "--root", str(root), "--experiment", spec, "--only", str(mix),
               "--llc-sets", str(LLC_SETS), "--llc-ways", str(LLC_WAYS),
               "--warmup", "%g" % w, "--sim", "%g" % s, "--results-dir", str(d), "--jobs", "1", "--no-verify"] \
            + (["--repl", "lru"] if lru else []) + self.a.runner_args
        (d / "oracle_command.txt").write_text(" ".join(cmd) + "\n")
        t0 = time.time()
        with open(d / "runner_output.txt", "w") as f:
            p = subprocess.Popen(cmd, cwd=str(root), stdout=f, stderr=subprocess.STDOUT)
            with pool.plock:
                pool.procs.add(p)
            try:
                p.wait()
            finally:
                with pool.plock:
                    pool.procs.discard(p)
        wall = (time.time() - t0) / 60
        if pool.stop:
            return None, None
        log = find_log(d, mix)
        ok = log is not None and parse_log(log)["complete"]
        say("%-6s mix%02d %-44s %s  %.1f min" % (phase, mix, d.relative_to(self.out).parts[0] + "/" + d.name,
                                                 "OK" if ok else "FAILED (see %s)" % d, wall))
        return (log if ok else None), wall

    def sim(self, pool, prio, owner, mix, phase, key, spec, lru=False):
        """One simulation; identical requests (e.g. the shared references) are simulated once."""
        d = self.run_dir(owner, phase, mix, key)
        with self.flock:
            if str(d) not in self.futs:
                self.futs[str(d)] = pool.submit(prio, sum(self.lens(phase)), self.run_one, d, mix, phase, spec, lru)
            return self.futs[str(d)]

    def load_mixinfo(self):
        for f in self.out.rglob("mixes.csv"):
            try:
                for r in csv.DictReader(open(f)):
                    self.mixinfo[int(r["mix_id"])] = dict(
                        name=r["mix_name"], type=r["type"],
                        traces=[r["core%d_trace" % i] for i in range(NCORES)],
                        classes=[r["core%d_class" % i] for i in range(NCORES)])
                return
            except Exception:
                continue

    def record(self, pf, mix, phase, rnd, key, cfg, log, base, wall, role=""):
        if log is None and self.a.dry_run:
            return {}
        row = dict(prefetcher=pf, mix=mix, phase=phase, round=rnd, config=key, role=role,
                   ways=("" if cfg is None else ("UCP adaptive" if cfg[0] == "A" else vtxt(cfg[0]))),
                   degree=("" if cfg is None else vtxt(cfg[1])), status="OK" if log else "MISSING",
                   wall_min=round(wall, 1) if wall else "", log=str(log or ""))
        if cfg is not None:
            row["_cfg"] = cfg
        if log and base:
            L = parse_log(log)
            m = metrics(L, parse_log(base))
            for i in range(NCORES):
                row["ipc_c%d" % i] = round(m["ipc"][i], 5)
            for i in range(NCORES):
                row["speedup_c%d" % i] = round(m["speedup"][i], 4)
            for k in ("NWS", "worst_core", "HS", "casualties", "sum_ipc_ratio", "DRAM_RPKI", "LLC_demand_MPKI",
                      "pf_accuracy", "LLC_miss_latency", "LLC_unused_pf_share", "DRAM_busy_gt50pct"):
                v = m.get(k)
                row[k] = round(v, 4) if isinstance(v, float) else v
            want = int(round(self.lens(phase)[1] * 1e6))
            if L["sim"] and L["sim"] != want:
                row["status"] = "OK (length differs: %s)" % L["sim"]
            if (L["llc_sets"], L["llc_ways"]) != (LLC_SETS, LLC_WAYS):
                row["status"] = "WRONG LLC %sx%s (expected %dx%d)" % (L["llc_sets"], L["llc_ways"], LLC_SETS, LLC_WAYS)
        with self.rlock:
            self.rows.append(row)
        return row

    # ---- one mix of one prefetcher: search, then validation
    def mix_driver(self, pool, k, pf, mix, deadline):
        nat = (NATIVE,) * NCORES
        base_cfg = ("A", nat)
        P = lambda p: (k, p)                                     # Pythia (k = 0) before IPCP (k = 1)
        log_b, wall = self.sim(pool, P(0), pf, mix, "search", cfg_tag(base_cfg), cfg_spec(pf, base_cfg)).result()
        self.record(pf, mix, "search", 0, cfg_tag(base_cfg), base_cfg, log_b, log_b, wall, "baseline")
        if not log_b:
            if not self.a.dry_run and not pool.stop:
                say("[%s] mix%02d: baseline failed, mix skipped" % (pf, mix))
            return
        seed = seed_split(parse_log(log_b))
        cur, cur_score = base_cfg, 1.0
        evaluated = {base_cfg: dict(NWS=1.0, worst_core=1.0)}
        path = [dict(prefetcher=pf, mix=mix, round=0, config=cfg_tag(base_cfg), NWS=1.0, worst_core=1.0,
                     note="baseline; UCP's most frequent split = %s" % vtxt(seed))]
        n_final = 1 + self.a.top + len(self.a.contexts)
        for rnd in range(1, self.a.rounds + 1):
            cands = [c for c in neighbours(cur, seed, self.a.levels, self.a.moves, rnd == 1) if c not in evaluated]
            if not cands:
                break
            if rnd > 1 and not self.a.dry_run:               # time guard: this round + the validation must still fit
                work = pool.pending_m + len(cands) * sum(self.a.search_len) + n_final * sum(self.a.final_len)
                if time.time() + work * pool.rate(self.a.rate) / pool.n * 60 > deadline:
                    path.append(dict(prefetcher=pf, mix=mix, round=rnd, config="-", note="skipped: time budget"))
                    break
            futs = [(c, self.sim(pool, P(10 - rnd), pf, mix, "search", cfg_tag(c), cfg_spec(pf, c))) for c in cands]
            best = None
            for c, fu in futs:
                log, wall = fu.result()
                row = self.record(pf, mix, "search", rnd, cfg_tag(c), c, log, log_b, wall)
                if log and row:
                    evaluated[c] = dict(NWS=row["NWS"], worst_core=row["worst_core"])
                    if best is None or row["NWS"] > evaluated[best]["NWS"]:
                        best = c
            if best is not None and evaluated[best]["NWS"] > cur_score + self.a.min_gain:
                cur, cur_score = best, evaluated[best]["NWS"]
                path.append(dict(prefetcher=pf, mix=mix, round=rnd, config=cfg_tag(cur), NWS=cur_score,
                                 worst_core=evaluated[cur]["worst_core"], note="accepted (%d tried)" % len(cands)))
            else:
                path.append(dict(prefetcher=pf, mix=mix, round=rnd, config=cfg_tag(best) if best else "-",
                                 NWS=evaluated[best]["NWS"] if best else None,
                                 note="no improvement -> stop (%d tried)" % len(cands)))
                break
        with self.rlock:
            self.paths.extend(path)
        # ---- finalists: best throughput + fairest that do not lose throughput, filled up to --top by throughput
        others = [c for c in evaluated if c != base_cfg]
        by_nws = sorted(others, key=lambda c: -evaluated[c]["NWS"])
        n_thr = max(1, self.a.top - 2)
        pick = by_nws[:n_thr]
        fair = sorted([c for c in others if c not in pick and evaluated[c]["NWS"] >= 1.0],
                      key=lambda c: (-evaluated[c]["worst_core"], -evaluated[c]["NWS"]))
        pick += fair[:self.a.top - len(pick)]
        pick += [c for c in by_nws if c not in pick][:self.a.top - len(pick)]
        # ---- validation
        jobs = [("baseline", base_cfg, pf, cfg_tag(base_cfg), cfg_spec(pf, base_cfg), False)]
        for i, c in enumerate(pick, 1):
            jobs.append(("finalist %d (%s)" % (i, "throughput" if i <= n_thr else "fairness"), c, pf, cfg_tag(c),
                         cfg_spec(pf, c), False))
        for ctx in self.a.contexts:
            exp, lru, shared = CONTEXTS[ctx]
            jobs.append((ctx, None, "common" if shared else pf, ctx, exp or PFS[pf]["lru_pf"], lru))
        futs = [(j, self.sim(pool, P(0), j[2], mix, "final", j[3], j[4], j[5])) for j in jobs]
        logs = [(j, fu.result()) for j, fu in futs]
        base_f = logs[0][1][0]
        for (role, c, _, key, _, _), (log, wall) in logs:
            self.record(pf, mix, "final", "", key, c, log, base_f, wall, role)
        if not pool.stop:
            say("[%s] mix%02d done: best searched NWS %.3f (%s)" % (pf, mix, cur_score, cfg_tag(cur)))

    # ---- plan / estimate
    def estimate(self, pfs, mixes):
        nat = (NATIVE,) * NCORES
        seed = (1, 6, 1, 8)
        r1 = len(neighbours(("A", nat), seed, self.a.levels, self.a.moves, True))
        rn = len(neighbours((seed, nat), seed, self.a.levels, self.a.moves, False))
        n_s = 1 + r1 + (self.a.rounds - 1) * rn
        n_shared = sum(CONTEXTS[c][2] for c in self.a.contexts)
        n_f = 1 + self.a.top + len(self.a.contexts) - n_shared
        ls, lf = sum(self.a.search_len), sum(self.a.final_len)
        tot_m = len(pfs) * len(mixes) * (n_s * ls + n_f * lf) + len(mixes) * n_shared * lf
        hours = tot_m * self.a.rate / self.a.jobs / 60
        print("Per prefetcher: %d mixes x (up to %d search runs at %g+%gM, %d validation runs at %g+%gM per core)"
              % (len(mixes), n_s, *self.a.search_len, n_f, *self.a.final_len))
        print("  + %d shared no-prefetcher references per mix at the validation length" % n_shared)
        print("  total at most %d simulations; about %.1f h with --jobs %d at %.2f min per M instructions "
              "(budget --hours %g; round %d+ of a mix is skipped when the budget is short)"
              % (len(pfs) * len(mixes) * (n_s + n_f) + len(mixes) * n_shared, hours, self.a.jobs, self.a.rate,
                 self.a.hours, 2))
        return hours

    # ------------------------------------------------------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------------------------------------------------------
    def write_reports(self):
        self.load_mixinfo()
        rows = sorted([r for r in self.rows if r], key=lambda r: (r["prefetcher"], r["mix"], r["phase"] != "final",
                                                                 str(r["round"]), r["config"]))
        if not rows:
            return
        keys = []
        for r in rows:
            for k in r:
                if k not in keys and not k.startswith("_"):
                    keys.append(k)
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.out / "oracle_runs.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        an = self.analyses(rows)
        with open(self.out / "oracle_summary.json", "w") as f:
            json.dump(an["summary"], f, indent=1, default=str)
        self.write_xlsx(rows, keys, an)
        self.plots(rows, an)
        say("reports written to %s" % self.out)

    def cls(self, mix, i):
        mi = self.mixinfo.get(mix)
        return (mi["traces"][i], mi["classes"][i]) if mi else ("?", "?")

    def analyses(self, rows):
        A = dict(summary=[], knobs=[], selfnb=[], ways=[], inter=[], pattern=[], pattern_cls=[], casual=[])
        nat = (NATIVE,) * NCORES
        for pf in [p for p in PFS if any(r["prefetcher"] == p for r in rows)]:
            R = [r for r in rows if r["prefetcher"] == pf]
            fin = [r for r in R if r["phase"] == "final" and r.get("NWS") is not None]
            srch = [r for r in R if r["phase"] == "search" and r.get("NWS") is not None]
            per = []
            for m in sorted({r["mix"] for r in R}):
                # -- knob headroom at the search length: prefetch only / partition only / both
                S = [r for r in srch if r["mix"] == m and "_cfg" in r]
                kinds = {"prefetch only (UCP adaptive ways)": [], "partition only (native prefetcher)": [],
                         "both knobs (coordination)": []}
                for r in S:
                    w, d = r["_cfg"]
                    if w == "A" and d != nat:
                        kinds["prefetch only (UCP adaptive ways)"].append(r)
                    elif w != "A" and d == nat:
                        kinds["partition only (native prefetcher)"].append(r)
                    elif w != "A" and d != nat:
                        kinds["both knobs (coordination)"].append(r)
                kr = dict(prefetcher=pf, mix=m)
                for kk, lst in kinds.items():
                    b = max(lst, key=lambda r: r["NWS"]) if lst else None
                    kr[kk] = b["NWS"] if b else None
                    kr[kk + " config"] = b["config"] if b else ""
                    kr[kk + " tried"] = len(lst)
                vals = {kk: kr[kk] for kk in kinds if kr[kk] is not None}
                kr["winner"] = max(vals, key=vals.get) if vals else ""
                A["knobs"].append(kr)
                # -- self vs neighbour (round 1: one core's degree changed, ways adaptive)
                for r in S:
                    w, d = r["_cfg"]
                    diff = [i for i in range(NCORES) if d[i] != NATIVE]
                    if w == "A" and len(diff) == 1:
                        i = diff[0]
                        oth = [r["speedup_c%d" % j] for j in range(NCORES) if j != i]
                        tr, cl = self.cls(m, i)
                        A["selfnb"].append(dict(prefetcher=pf, mix=m, core=i, trace=tr, cls=cl, degree_cap=d[i],
                                                self_speedup=r["speedup_c%d" % i],
                                                neighbours_mean=round(sum(oth) / len(oth), 4), neighbours_min=min(oth),
                                                NWS=r["NWS"]))
                # -- ways moves (pinned split, native prefetcher): who gains, who loses
                base_seed = next((p["note"].split("= ")[-1] for p in self.paths
                                  if p["prefetcher"] == pf and p["mix"] == m and p["round"] == 0), None)
                seed = tuple(int(x) for x in base_seed.split("-")) if base_seed else None
                for r in S:
                    w, d = r["_cfg"]
                    if seed and w != "A" and d == nat and len(w) == NCORES:
                        delta = [w[i] - seed[i] for i in range(NCORES)]
                        dn = [i for i in range(NCORES) if delta[i] < 0]
                        up = [i for i in range(NCORES) if delta[i] > 0]
                        if len(dn) == 1 and len(up) == 1:
                            i, j = dn[0], up[0]
                            A["ways"].append(dict(prefetcher=pf, mix=m, ways=vtxt(w), moved=delta[j],
                                                  donor_core=i, donor_class=self.cls(m, i)[1],
                                                  donor_speedup=r["speedup_c%d" % i], receiver_core=j,
                                                  receiver_class=self.cls(m, j)[1],
                                                  receiver_speedup=r["speedup_c%d" % j], NWS=r["NWS"]))
                # -- validated results of this mix
                F = {r["role"]: r for r in fin if r["mix"] == m}
                finals = [r for rl, r in F.items() if rl.startswith("finalist")]
                if not finals or "baseline" not in F:
                    continue
                b = max(finals, key=lambda r: r["NWS"])
                fair = max([r for r in finals if r["NWS"] >= 1.0] or finals, key=lambda r: r["worst_core"])
                sv = {r["config"]: r["NWS"] for r in S}
                pairs = [(sv[r["config"]], r["NWS"]) for r in finals if r["config"] in sv]
                pm = dict(mix=m, best_config=b["config"], best_NWS=b["NWS"], best_worst_core=b["worst_core"],
                          fair_config=fair["config"], fair_NWS=fair["NWS"], fair_worst_core=fair["worst_core"],
                          rank_agreement=spearman(pairs))
                # -- interaction and casualties against the no-mechanism reference (plain LRU, no prefetcher)
                if all(x in F for x in ("lru_nopf", "lru_pf", "ucp_nopf")):
                    ref = F["lru_nopf"]
                    cas_b = cas_o = 0
                    for i in range(NCORES):
                        s = lambda r: r["speedup_c%d" % i] / ref["speedup_c%d" % i]
                        pf_a, ucp_a, both, best = s(F["lru_pf"]), s(F["ucp_nopf"]), s(F["baseline"]), s(b)
                        tr, cl = self.cls(m, i)
                        A["inter"].append(dict(prefetcher=pf, mix=m, core=i, trace=tr, cls=cl,
                                               prefetch_alone=round(pf_a, 4), ucp_alone=round(ucp_a, 4),
                                               independent_both=round(both, 4), product=round(pf_a * ucp_a, 4),
                                               interaction=round(both / (pf_a * ucp_a), 4),
                                               best_config=round(best, 4),
                                               casualty_independent=both < 0.98, casualty_best=best < 0.98))
                        cas_b += both < 0.98
                        cas_o += best < 0.98
                    pm["casualties_independent"], pm["casualties_best"] = cas_b, cas_o
                per.append(pm)
                # -- winning policy per core
                if "_cfg" in b:
                    w, d = b["_cfg"]
                    for i in range(NCORES):
                        tr, cl = self.cls(m, i)
                        A["pattern"].append(dict(prefetcher=pf, mix=m, core=i, trace=tr, cls=cl, degree_cap=d[i],
                                                 ways="adaptive" if w == "A" else w[i],
                                                 speedup=b["speedup_c%d" % i]))
            if per:
                n = len(per)
                n_gain = sum(p["best_NWS"] >= 1.03 for p in per)
                n_fair = sum(p["fair_worst_core"] >= 1.05 and p["fair_NWS"] >= 1.0 for p in per)
                kn = [k for k in A["knobs"] if k["prefetcher"] == pf]
                A["summary"].append(dict(
                    prefetcher=pf, mixes=n, mean_best_NWS=sum(p["best_NWS"] for p in per) / n,
                    max_best_NWS=max(p["best_NWS"] for p in per), mixes_gain_ge_3pct=n_gain,
                    mixes_worst_core_up_5pct_no_loss=n_fair,
                    casualties_independent=sum(p.get("casualties_independent", 0) for p in per),
                    casualties_best=sum(p.get("casualties_best", 0) for p in per),
                    mixes_where_both_knobs_win=sum(k["winner"].startswith("both") for k in kn),
                    verdict="ROOM FOR COORDINATION" if (n_gain * 3 >= n or n_fair * 3 >= n) else "no clear room (static)",
                    per_mix=per))
            # -- winning policy aggregated per class
            pat = [p for p in A["pattern"] if p["prefetcher"] == pf]
            for c in sorted({p["cls"] for p in pat}):
                ps = [p for p in pat if p["cls"] == c]
                wv = [p["ways"] for p in ps if p["ways"] != "adaptive"]
                A["pattern_cls"].append(dict(prefetcher=pf, cls=c, cores=len(ps),
                                             mean_degree_cap=round(sum(p["degree_cap"] for p in ps) / len(ps), 2),
                                             share_prefetch_off=round(sum(p["degree_cap"] == 0 for p in ps) / len(ps), 2),
                                             share_native=round(sum(p["degree_cap"] == NATIVE for p in ps) / len(ps), 2),
                                             mean_ways_when_pinned=round(sum(wv) / len(wv), 2) if wv else "",
                                             mean_speedup=round(sum(p["speedup"] for p in ps) / len(ps), 4)))
        return A

    def write_xlsx(self, rows, keys, A):
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
        except ImportError:
            say("openpyxl not installed: only oracle_runs.csv / oracle_summary.json were written")
            return
        bold = Font(bold=True)
        wb = Workbook()
        ws = wb.active
        ws.title = "Summary"

        def table(ws, hdr, data, note=None):
            if note:
                ws.append([note])
            ws.append(hdr)
            for c in ws[ws.max_row]:
                c.font = bold
            for d in data:
                ws.append([round(v, 4) if isinstance(v, float) else v for v in d])
            ws.append([])

        ws.append(["Static oracle study: best fixed per-core prefetch degree x LLC ways vs the independent baseline "
                   "(UCP adaptive + native prefetcher), coord-controller tree"])
        ws["A1"].font = bold
        ws.append(["Success criterion (fixed before running): best validated config >= +3% NWS in at least 1/3 of the "
                   "mixes, OR worst core >= +5% without losing NWS in at least 1/3 of the mixes"])
        ws.append(["search %g+%gM, validation %g+%gM per core; degree caps %s (%d = native, 0 = off); way moves %s; %d "
                   "rounds max" % (*self.a.search_len, *self.a.final_len, vtxt(self.a.levels), NATIVE,
                                   vtxt(self.a.moves), self.a.rounds)])
        ws.append([])
        table(ws, ["prefetcher", "mixes", "mean best NWS", "max best NWS", "mixes >= +3%", "mixes worst core +5% (no loss)",
                   "mixes where both knobs win", "casualty cores: independent", "casualty cores: best config", "verdict"],
              [[s["prefetcher"], s["mixes"], s["mean_best_NWS"], s["max_best_NWS"], s["mixes_gain_ge_3pct"],
                s["mixes_worst_core_up_5pct_no_loss"], s["mixes_where_both_knobs_win"], s["casualties_independent"],
                s["casualties_best"], s["verdict"]] for s in A["summary"]])
        table(ws, ["prefetcher", "mix", "type", "best config (throughput)", "NWS", "worst core", "fairest config (no loss)",
                   "NWS", "worst core", "casualties independent", "casualties best", "rank agreement search/validation"],
              [[s["prefetcher"], p["mix"], self.mixinfo.get(p["mix"], {}).get("type", ""), p["best_config"],
                p["best_NWS"], p["best_worst_core"], p["fair_config"], p["fair_NWS"], p["fair_worst_core"],
                p.get("casualties_independent", ""), p.get("casualties_best", ""), p["rank_agreement"]]
               for s in A["summary"] for p in s["per_mix"]])
        for t in ("config names: w<ways>_d<degree caps>; wucp = UCP adaptive ways; degree %d = native, 0 = prefetcher off" % NATIVE,
                  "NWS = mean over cores of IPC / IPC in the baseline run (same mix, same length); worst core = min of those",
                  "casualty = core slower than on a plain LRU LLC without prefetching (speedup < 0.98)"):
            ws.append([t])

        def sheet(name, data, note):
            w2 = wb.create_sheet(name)
            w2.append([note])
            w2["A1"].font = bold
            if data:
                hdr = list(data[0].keys())
                w2.append(hdr)
                for c in w2[2]:
                    c.font = bold
                for d in data:
                    w2.append([d.get(k, "") if not isinstance(d.get(k), float) else round(d[k], 4) for k in hdr])
            w2.freeze_panes = "A3"

        sheet("Knob headroom", A["knobs"], "Best NWS at the search length when only the prefetcher is tuned, only the "
              "partition is tuned, or both: if 'both' wins, the knobs must be set together (coordination)")
        sheet("Self vs neighbour", A["selfnb"], "One core's prefetcher capped / off (round 1): its own speedup vs its "
              "neighbours' -- self-harm (self > 1 when off) vs harm to neighbours (neighbours > 1 when off)")
        sheet("Way moves", A["ways"], "Ways moved from UCP's own split (native prefetcher): donor vs receiver speedup "
              "= how much each class values its ways next to the prefetcher")
        sheet("Interaction", A["inter"], "Per core, against plain LRU without prefetching (validation length): prefetcher "
              "alone, UCP alone, both independent; interaction = both / (prefetch alone x UCP alone); < 1 = they hurt "
              "each other")
        sheet("Winning policy", A["pattern"], "Per core of every mix: what the best validated configuration does")
        sheet("Policy per class", A["pattern_cls"], "The best configurations aggregated by core class: what a "
              "coordinator would have to learn")
        for name, ph in (("Final (validated)", "final"), ("All runs", None)):
            w2 = wb.create_sheet(name)
            w2.append(keys)
            for c in w2[1]:
                c.font = bold
            for r in rows:
                if ph is None or r["phase"] == ph:
                    w2.append([r.get(k, "") for k in keys])
            w2.freeze_panes = "A2"
        w3 = wb.create_sheet("Search path")
        pk = ["prefetcher", "mix", "round", "config", "NWS", "worst_core", "note"]
        w3.append(pk)
        for c in w3[1]:
            c.font = bold
        for p in sorted(self.paths, key=lambda p: (p["prefetcher"], p["mix"], p["round"])):
            w3.append([p.get(k, "") for k in pk])
        hi = PatternFill("solid", fgColor="E5F4E4")
        wf = wb["Final (validated)"]
        if "NWS" in keys:
            ci = keys.index("NWS")
            for row in wf.iter_rows(min_row=2):
                v = row[ci].value
                if isinstance(v, (int, float)) and v >= 1.03:
                    row[ci].fill = hi
        wb.save(self.out / "oracle_report.xlsx")

    def plots(self, rows, A):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return
        for pf in sorted({r["prefetcher"] for r in rows}):
            mixes = sorted({r["mix"] for r in rows if r["prefetcher"] == pf})
            n = len(mixes)
            cols = min(5, n)
            nr = math.ceil(n / cols)
            fig, axs = plt.subplots(nr, cols, figsize=(3.8 * cols, 3.3 * nr), squeeze=False)
            for ax, m in zip(axs.flat, mixes):
                s = [r for r in rows if r["prefetcher"] == pf and r["mix"] == m and r["phase"] == "search" and r.get("NWS")]
                f = [r for r in rows if r["prefetcher"] == pf and r["mix"] == m and r["phase"] == "final" and r.get("NWS")]
                ax.scatter([r["worst_core"] for r in s], [r["NWS"] for r in s], s=10, c="#9aa5b1", label="searched (short)")
                fin = [r for r in f if r["role"].startswith("finalist")]
                ax.scatter([r["worst_core"] for r in fin], [r["NWS"] for r in fin], s=36, c="#2a7ab9",
                           label="finalists (validated)")
                for r in f:
                    if r["role"] in CONTEXTS:
                        ax.scatter([r["worst_core"]], [r["NWS"]], s=36, marker="s", c="#e0a030")
                        ax.annotate(r["role"], (r["worst_core"], r["NWS"]), fontsize=6)
                ax.scatter([1.0], [1.0], s=90, marker="*", c="#c0392b", label="independent baseline", zorder=5)
                ax.axhline(1.0, lw=0.6, c="#c0392b")
                ax.axvline(1.0, lw=0.6, c="#c0392b")
                ax.set_title("mix%02d %s" % (m, self.mixinfo.get(m, {}).get("name", "")[6:]), fontsize=8)
                ax.set_xlabel("worst-core speedup", fontsize=7)
                ax.set_ylabel("NWS vs baseline", fontsize=7)
                ax.tick_params(labelsize=6)
            for ax in list(axs.flat)[n:]:
                ax.axis("off")
            axs.flat[0].legend(fontsize=5, loc="best")
            fig.suptitle("%s: every fixed configuration vs the independent baseline (up/right of the star = better)"
                         % PFS[pf]["name"], fontsize=10)
            fig.tight_layout()
            fig.savefig(self.out / ("oracle_%s.png" % pf), dpi=150)
            plt.close(fig)
        if A["knobs"]:
            kinds = ["prefetch only (UCP adaptive ways)", "partition only (native prefetcher)", "both knobs (coordination)"]
            pfs = sorted({k["prefetcher"] for k in A["knobs"]})
            fig, axs = plt.subplots(len(pfs), 1, figsize=(12, 3.2 * len(pfs)), squeeze=False)
            for ax, pf in zip(axs.flat, pfs):
                kn = [k for k in A["knobs"] if k["prefetcher"] == pf]
                x = range(len(kn))
                for j, (kk, col) in enumerate(zip(kinds, ("#2a7ab9", "#e0a030", "#2e8b57"))):
                    ax.bar([i + (j - 1) * 0.27 for i in x], [(k[kk] or 1.0) - 1.0 for k in kn], 0.27, label=kk, color=col)
                ax.axhline(0, c="k", lw=0.6)
                ax.set_xticks(list(x))
                ax.set_xticklabels(["mix%02d" % k["mix"] for k in kn], fontsize=7)
                ax.set_ylabel("best NWS - 1", fontsize=8)
                ax.set_title("%s: where is the headroom? (search length)" % PFS[pf]["name"], fontsize=9)
                ax.legend(fontsize=7)
            fig.tight_layout()
            fig.savefig(self.out / "oracle_knobs.png", dpi=150)
            plt.close(fig)


def spearman(pairs):
    if len(pairs) < 3:
        return None

    def ranks(v):
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0] * len(v)
        for k, i in enumerate(o):
            r[i] = k
        return r
    a, b = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    return round(1 - 6 * sum((x - y) ** 2 for x, y in zip(a, b)) / (n * (n * n - 1)), 3)


def parse_len(text):
    w, _, s = text.partition("+")
    return float(w), float(s)


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--root", type=Path, default=here.parent if here.name == "scripts" else Path.cwd(),
                    help="ChampSim repository root (coord-controller based branch)")
    ap.add_argument("--prefetchers", default="pythia,ipcp", help="in priority order")
    ap.add_argument("--mixes", default=DEFAULT_MIXES, help="mix ids (same 50-mix generator and seed as the runner)")
    ap.add_argument("-j", "--jobs", type=int, default=100, help="simulations running at the same time")
    ap.add_argument("--search-len", type=parse_len, default=(5, 25), metavar="W+S", help="search length, M instr. per core")
    ap.add_argument("--final-len", type=parse_len, default=(25, 100), metavar="W+S", help="validation length, M instr. per core")
    ap.add_argument("--rounds", type=int, default=2, help="greedy search rounds after the baseline")
    ap.add_argument("--top", type=int, default=5, help="configurations validated at the final length")
    ap.add_argument("--levels", default="0,1,2,4", help="degree caps tried besides native (%d)" % NATIVE)
    ap.add_argument("--way-moves", default="2,4", help="numbers of ways moved from one core to another")
    ap.add_argument("--contexts", default="ucp_nopf,lru_pf,lru_nopf", help="reference points (%s)" % ", ".join(CONTEXTS))
    ap.add_argument("--min-gain", type=float, default=0.002, help="a search step must improve NWS by more than this")
    ap.add_argument("--hours", type=float, default=9.0, help="time budget for everything")
    ap.add_argument("--rate", type=float, default=RATE_MIN_PER_M, help="min per M instructions per simulation (first guess)")
    ap.add_argument("--sequential", action="store_true", help="start IPCP only when Pythia is completely finished "
                    "(default: IPCP uses the threads Pythia leaves idle; Pythia always has priority)")
    ap.add_argument("--out", type=Path, default=None, help="results folder (default <root>/oracle_results)")
    ap.add_argument("--no-build", action="store_true", help="do not (re)build the binaries first")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the time estimate, run nothing")
    ap.add_argument("--report-only", action="store_true", help="only rebuild the reports from the logs already there")
    ap.add_argument("--runner-arg", action="append", default=[], dest="runner_args",
                    help="extra argument for every runner call (repeat), e.g. --runner-arg=--trace-dir=/data/traces")
    a = ap.parse_args()

    a.root = a.root.expanduser().resolve()
    a.out = a.out or a.root / "oracle_results"
    pfs = [p.strip() for p in a.prefetchers.split(",") if p.strip()]
    for p in pfs:
        if p not in PFS:
            ap.error("unknown prefetcher %s (pythia, ipcp)" % p)
    if not (a.root / RUNNER).is_file():
        ap.error("%s not found in %s (run from the oracle-study branch root or give --root)" % (RUNNER, a.root))
    mixes = sorted({int(x) for x in re.split(r"[,\s]+", a.mixes) if x})
    a.contexts = [c for c in a.contexts.split(",") if c]
    for c in a.contexts:
        if c not in CONTEXTS:
            ap.error("unknown context %s" % c)
    a.levels = tuple(sorted({int(x) for x in a.levels.split(",") if x} | {NATIVE}))
    a.moves = tuple(sorted({int(x) for x in a.way_moves.split(",") if x}))
    st = Study(a)

    print("Static oracle study on %s: prefetchers %s, mixes %s, jobs %d" % (a.root, " then ".join(pfs),
                                                                            ",".join(map(str, mixes)), a.jobs))
    st.estimate(pfs, mixes)
    if a.dry_run:
        nat = ("A", (NATIVE,) * NCORES)
        for p in pfs:
            print("\n[%s] baseline:   --experiment %s" % (p, cfg_spec(p, nat)))
            for c in neighbours(nat, (1, 6, 1, 8), a.levels, a.moves, True)[:2] + [((1, 6, 1, 8), (6, 0, 6, 2))]:
                print("[%s] e.g.       --experiment %-42s (%s)" % (p, cfg_spec(p, c), cfg_tag(c)))
            for c in a.contexts:
                exp, lru, _ = CONTEXTS[c]
                print("[%s] reference:  --experiment %s%s" % (p, exp or PFS[p]["lru_pf"], " --repl lru" if lru else ""))
        return

    if not a.report_only and not a.no_build:
        specs = sorted({PFS[p]["base"] for p in pfs} | {PFS[p]["base"] + "+deg=2" for p in pfs}
                       | {(CONTEXTS[c][0] or PFS[p]["lru_pf"]) for c in a.contexts for p in pfs})
        cmd = [sys.executable, str(a.root / RUNNER), "--root", str(a.root), "--experiment", ",".join(specs),
               "--llc-sets", str(LLC_SETS), "--llc-ways", str(LLC_WAYS),
               "--repl", "lru", "--build", "--build-only"] + a.runner_args
        say("building the binaries: %s" % " ".join(cmd))
        if subprocess.call(cmd, cwd=str(a.root)) != 0:
            sys.exit("[ERROR] build failed")
    if a.report_only:
        a.dry_run = True                    # reuse logs only, never simulate

    pool = Pool(a.jobs)

    def _stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    t0 = time.time()
    deadline = t0 + a.hours * 3600
    say("start; time budget until %s" % datetime.fromtimestamp(deadline).strftime("%a %H:%M"))
    try:
        groups = []
        for k, p in enumerate(pfs):
            ds = [threading.Thread(target=st.mix_driver, args=(pool, k, p, m, deadline), daemon=True) for m in mixes]
            groups.append((p, ds))
        for p, ds in groups:
            for d in ds:
                d.start()
            if a.sequential or a.report_only:
                while any(d.is_alive() for d in ds):
                    time.sleep(2)
        for p, ds in groups:
            while any(d.is_alive() for d in ds):
                time.sleep(2)
            say("[%s] all mixes finished (%.1f h since start)" % (p, (time.time() - t0) / 3600))
            st.write_reports()
    except KeyboardInterrupt:
        say("interrupted: stopping the running simulations (finished ones are kept; re-run the same command to resume)")
        pool.kill()
        time.sleep(3)
        st.write_reports()
        sys.exit(130)
    for s in st.analyses(st.rows)["summary"]:
        print("%s: %d mixes, mean best NWS %.3f, max %.3f, %d mixes >= +3%%, %d mixes worst core +5%%, casualty cores "
              "%d -> %d, both knobs win in %d mixes -> %s"
              % (s["prefetcher"], s["mixes"], s["mean_best_NWS"], s["max_best_NWS"], s["mixes_gain_ge_3pct"],
                 s["mixes_worst_core_up_5pct_no_loss"], s["casualties_independent"], s["casualties_best"],
                 s["mixes_where_both_knobs_win"], s["verdict"]))


if __name__ == "__main__":
    main()
