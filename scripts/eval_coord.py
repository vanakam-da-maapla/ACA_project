#!/usr/bin/env python3
"""
eval_coord.py -- evaluate the coordination controller (COORD_POLICY=victim, src/coord.cc) against independent operation
=======================================================================================================================
Set-ups: 4core (normal bandwidth, oracle mixes), 4core_lowbw (same mixes, 2.4 GB/s per core like the 8-core
low-bandwidth runs; base and ref are simulated here), 8core (low bandwidth, the 8 mixes with the most casualties).
All binaries are built and run with the name suffix -coord (--bin-tag), so a running study is never disturbed.
Compares, per mix and per prefetcher:
    base   UCP + native prefetcher (independent operation)
    coord  UCP + prefetcher + the controller (one or more parameter variants)
    ref    plain LRU, no prefetcher (to count casualty cores and to express everything as speedups)
on the two set-ups of the earlier studies, re-using every run that already exists:
    4core  the 14 mixes of the 4-core oracle (25M+100M, 2 channels DDR4-2400); base and ref come from
           oracle_results/ and the oracle's best validated configuration is shown as the target
    8core  the 20 mixes of 4to8core_scaleup_exp.py at LOW bandwidth (1200 MT/s, 25M+150M); base and ref come from
           scaleup_8core_results/tier1/ (a mix whose base/ref run is not finished yet is skipped unless --run-missing)

ONE COMMAND (repository root):
    nohup python3 scripts/eval_coord.py --setups 4core,8core --jobs 60 > eval.log 2>&1 &
    python3 scripts/eval_coord.py --dry-run            # what would run
    python3 scripts/eval_coord.py --report-only        # workbook from the logs already there
Variants: --variant NAME=KNOBS adds a controller variant, KNOBS = runner knobs, e.g.
    --variant gain1pct=+cv=V_GAIN:0.01   --variant fast=+cv=EPOCH_CYCLES:500000
(default: one variant 'vpt' = the controller with its default parameters).  Outputs: eval_results/eval_report.xlsx, eval_*.csv.
"""
import argparse
import csv
import heapq
import importlib.util
import itertools
import math
import re
import signal
import statistics as st
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

RUNNER = "scripts/run_4core_mixes_fixingKnob.py"
PFS = {"pythia": "ucp_pythia", "ipcp": "ucp_ipcp"}
SETUPS = {
    "4core": dict(cores=4, sets=2048, ways=16, warm=25, sim=100, bw="", mixes=[1, 3, 5, 7, 9, 12, 13, 16, 17, 19, 24, 28, 33, 49]),
    # 4 cores with the per-core DRAM bandwidth of the 8-core low-bandwidth runs (2 channels at 600 MT/s = 2.4 GB/s/core)
    "4core_lowbw": dict(cores=4, sets=2048, ways=16, warm=25, sim=100, bw="+bw=600",
                        mixes=[1, 3, 5, 7, 9, 12, 13, 16, 17, 19, 24, 28, 33, 49]),
    # default: the 8 low-bandwidth 8-core mixes with the most casualty cores (02 14 16 15 09 11 20 06)
    "8core": dict(cores=8, sets=4096, ways=16, warm=25, sim=150, bw="+bw=1200", mixes=[2, 6, 9, 11, 14, 15, 16, 20]),
}
LOCK = threading.Lock()


def say(m):
    with LOCK:
        print("%s %s" % (datetime.now().strftime("%H:%M:%S"), m), flush=True)


def find_log(d):
    for f in sorted(Path(d).glob("mix*/mix*.txt")):
        if f.stem == f.parent.name:
            return f
    return None


_PC = {}


def parse(path, n):
    k = (str(path), Path(path).stat().st_mtime)
    if k in _PC:
        return _PC[k]
    t = Path(path).read_text(errors="replace")

    def g(rx):
        m = re.search(rx, t, re.M)
        return float(m.group(1)) if m else None
    cores = []
    for c in range(n):
        p = r"^Core_%d_" % c
        cores.append(dict(ipc=g(p + r"IPC\s+(\S+)"), ins=g(p + r"instructions\s+(\d+)"),
                          dmiss=(g(p + r"LLC_load_miss\s+(\d+)") or 0) + (g(p + r"LLC_RFO_miss\s+(\d+)") or 0),
                          miss=g(p + r"LLC_total_miss\s+(\d+)") or 0, wbmiss=g(p + r"LLC_writeback_miss\s+(\d+)") or 0,
                          final_level=g(p + r"coord_final_level\s+(\d+)"),
                          lvl_epochs=[int(x) for x in (re.search(p + r"coord_epochs_at_level((?:\s+\d+)+)", t, re.M).group(1).split()
                                                       if re.search(p + r"coord_epochs_at_level", t, re.M) else [])]))
    vs = re.search(r"^COORD_VSUM (.*)$", t, re.M)
    bw = [g(r"^DRAM_bw_level_%d\s+(\d+)" % k) for k in range(4)]
    res = dict(cores=cores, vsum=vs.group(1) if vs else "", complete=all(c["ipc"] for c in cores),
               sim=g(r"^simulation_instructions\s+(\d+)"), sets=g(r"^llc_set\s+(\d+)\s*$"),
               busy50=(bw[2] + bw[3]) / sum(bw) if all(x is not None for x in bw) and sum(bw) else None)
    _PC[k] = res
    return res


class Job:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.log = None


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--root", type=Path, default=here.parent if here.name == "scripts" else Path.cwd())
    ap.add_argument("--setups", default="4core,4core_lowbw")
    ap.add_argument("--prefetchers", default="pythia,ipcp")
    ap.add_argument("--variant", action="append", default=[], help="NAME=KNOBS, e.g. gain1pct=+cv=V_GAIN:0.01")
    ap.add_argument("--mixes-4core", default=None, help="override the 4-core mix ids")
    ap.add_argument("--mixes-8core", default=None, help="override the 8-core mix ids")
    ap.add_argument("--bin-tag", default="coord", help="binary name suffix for every build and run")
    ap.add_argument("--oracle-dir", type=Path, default=None, help="default <root>/oracle_results")
    ap.add_argument("--scaleup-dir", type=Path, default=None, help="default <root>/scaleup_8core_results")
    ap.add_argument("--out", type=Path, default=None, help="default <root>/eval_results")
    ap.add_argument("-j", "--jobs", type=int, default=60)
    ap.add_argument("--run-missing", action="store_true", help="also simulate base/ref runs that are not there yet")
    ap.add_argument("--no-build", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--runner-arg", action="append", default=[], dest="runner_args")
    ap.add_argument("--test-len", default=None, help="testing only: W+S instead of the study lengths")
    a = ap.parse_args()
    if a.test_len:
        w, _, sm = a.test_len.partition("+")
        for S in SETUPS.values():
            S["warm"], S["sim"] = float(w), float(sm)
    root = a.root.resolve()
    odir = (a.oracle_dir or root / "oracle_results").resolve()
    sdir = (a.scaleup_dir or root / "scaleup_8core_results").resolve()
    out = (a.out or root / "eval_results").resolve()
    variants = [("vpt", "+coord=victim"), ("vpt_part", "+coord=victim+cv=V_PART:1")] + [(v.split("=", 1)[0], "+coord=victim" + v.split("=", 1)[1]) for v in a.variant]
    pfs = [p for p in a.prefetchers.split(",") if p]
    setups = [s for s in a.setups.split(",") if s]
    if a.mixes_4core:
        SETUPS["4core"]["mixes"] = [int(x) for x in a.mixes_4core.split(",")]
    if a.mixes_4core:
        SETUPS["4core_lowbw"]["mixes"] = SETUPS["4core"]["mixes"]
    if a.mixes_8core:
        SETUPS["8core"]["mixes"] = [int(x) for x in a.mixes_8core.split(",")]
    mixcsv8 = sdir / "mixes_8core.csv"
    if "8core" in setups and not mixcsv8.is_file():
        sys.exit("[ERROR] %s not found (the 8-core set-up re-uses the mixes of 4to8core_scaleup_exp.py)" % mixcsv8)

    # ---- plan
    jobs = []
    for s in setups:
        S = SETUPS[s]
        for p in pfs:
            for m in S["mixes"]:
                if s == "4core":
                    base_d = odir / p / "final_25+100M" / ("mix%02d" % m) / "wucp_d6-6-6-6"
                    ref_d = odir / "common" / "final_25+100M" / ("mix%02d" % m) / "lru_nopf"
                elif s == "4core_lowbw":                        # nothing to re-use: simulated here
                    base_d = out / s / p / "base" / ("mix%02d" % m)
                    ref_d = out / s / "common" / "ref" / ("mix%02d" % m)
                else:
                    base_d = sdir / "tier1" / ("ucp_%s_bw1200" % p) / ("mix%02d" % m)
                    ref_d = sdir / "tier1" / "lru_nopf_bw1200" / ("mix%02d" % m)
                own = out / s / p
                jobs.append(Job(setup=s, pf=p, mix=m, role="base", d=base_d, alt=own / "base" / ("mix%02d" % m),
                                spec=PFS[p] + S["bw"], lru=False, prio=1))
                jobs.append(Job(setup=s, pf=p, mix=m, role="ref", d=ref_d, alt=out / s / "common" / "ref" / ("mix%02d" % m),
                                spec="plain" + S["bw"], lru=True, prio=1))
                for vn, vk in variants:
                    jobs.append(Job(setup=s, pf=p, mix=m, role=vn, d=own / vn / ("mix%02d" % m), alt=None,
                                    spec=PFS[p] + vk + S["bw"], lru=False, prio=0))
    # dedupe (ref is shared by the prefetchers)
    uniq = {}
    for j in jobs:
        uniq.setdefault((str(j.d), j.role), j)
    todo = []
    for j in uniq.values():
        for d in (j.d, j.alt):
            S = SETUPS[j.setup]
            if d is not None and find_log(d):
                pl = parse(find_log(d), S["cores"])
                if pl["complete"] and pl["sim"] == S["sim"] * 1e6 and pl["sets"] == S["sets"]:   # same length and LLC
                    j.log = find_log(d)
                    break
        if j.log is None and (j.role in ("base", "ref") and not a.run_missing and j.setup != "4core_lowbw"):
            continue
        if j.log is None:
            todo.append(j)
    print("eval: setups %s, prefetchers %s, variants %s; %d runs to simulate (%d re-used)"
          % (setups, pfs, [v for v, _ in variants], len(todo), sum(j.log is not None for j in uniq.values())))
    if a.dry_run:
        for j in todo[:8]:
            print("  %s %s mix%02d %-8s --experiment %s" % (j.setup, j.pf, j.mix, j.role, j.spec))
        return

    stop = threading.Event()
    procs = set()

    def run(j):
        S = SETUPS[j.setup]
        d = j.d if j.role not in ("base", "ref") else j.alt
        d.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(root / RUNNER), "--root", str(root), "--experiment", j.spec, "--only", str(j.mix),
               "--warmup", str(S["warm"]), "--sim", str(S["sim"]), "--llc-sets", str(S["sets"]), "--llc-ways", str(S["ways"]),
               "--results-dir", str(d), "--jobs", "1", "--no-verify", "--bin-tag", a.bin_tag] \
            + (["--repl", "lru"] if j.lru else []) + a.runner_args
        if S["cores"] != 4:
            cmd += ["--cores", str(S["cores"]), "--mixes-csv", str(mixcsv8)]
        t0 = time.time()
        with open(d / "runner_output.txt", "w") as f:
            p = subprocess.Popen(cmd, cwd=str(root), stdout=f, stderr=subprocess.STDOUT)
            procs.add(p)
            p.wait()
            procs.discard(p)
        if stop.is_set():
            return
        lg = find_log(d)
        ok = lg is not None and parse(lg, S["cores"])["complete"]
        j.log = lg if ok else None
        say("%s %-6s mix%02d %-10s %s %.0f min" % (j.setup, j.pf, j.mix, j.role, "OK" if ok else "FAILED: %s" % d,
                                                   (time.time() - t0) / 60))

    if not a.report_only and todo:
        if not a.no_build:
            for s in setups:
                S = SETUPS[s]
                specs = sorted({PFS[p] for p in pfs} | {"plain"})
                cmd = [sys.executable, str(root / RUNNER), "--root", str(root), "--experiment", ",".join(specs),
                       "--repl", "lru", "--llc-sets", str(S["sets"]), "--llc-ways", str(S["ways"]), "--build",
                       "--build-only", "--bin-tag", a.bin_tag] + a.runner_args
                if S["cores"] != 4:
                    cmd += ["--cores", str(S["cores"]), "--mixes-csv", str(mixcsv8)]
                say("building %s binaries" % s)
                if subprocess.call(cmd, cwd=str(root)) != 0:
                    sys.exit("[ERROR] build failed")
        heap = [(j.prio, k, j) for k, j in enumerate(todo)]
        heapq.heapify(heap)
        hl = threading.Lock()

        def worker():
            while not stop.is_set():
                with hl:
                    if not heap:
                        return
                    _, _, j = heapq.heappop(heap)
                run(j)

        def _stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        ts = [threading.Thread(target=worker, daemon=True) for _ in range(a.jobs)]
        for t in ts:
            t.start()
        try:
            while any(t.is_alive() for t in ts):
                time.sleep(5)
        except KeyboardInterrupt:
            stop.set()
            for p in list(procs):
                p.send_signal(signal.SIGTERM)
            time.sleep(3)
    report(uniq.values(), variants, pfs, setups, odir, out)


def report(jobs, variants, pfs, setups, odir, out):
    by = defaultdict(dict)
    for j in jobs:
        if j.log:
            by[(j.setup, j.pf, j.mix)][j.role] = parse(j.log, SETUPS[j.setup]["cores"])
    # the no-prefetcher reference is the same run for every prefetcher of a mix: share it
    for (s_, p_, m_), R in list(by.items()):
        if "ref" in R:
            for p2 in PFS:
                if (s_, p2, m_) in by and "ref" not in by[(s_, p2, m_)]:
                    by[(s_, p2, m_)]["ref"] = R["ref"]
    oracle = {}
    oc = odir / "oracle_runs.csv"
    if oc.is_file():
        for r in csv.DictReader(open(oc)):
            if r["phase"] == "final" and r["role"].startswith("finalist") and r.get("NWS"):
                k = (r["prefetcher"], int(r["mix"]))
                oracle[k] = max(oracle.get(k, 0), float(r["NWS"]))
    mixrows, corerows, summ = [], [], []
    for (s, p, m), R in sorted(by.items()):
        if "base" not in R:
            continue
        n = SETUPS[s]["cores"]
        b, ref = R["base"], R.get("ref")
        for vn, _ in variants:
            if vn not in R:
                continue
            v = R[vn]
            sp = [x["ipc"] / y["ipc"] for x, y in zip(v["cores"], b["cores"])]
            row = dict(setup=s, prefetcher=p, mix=m, variant=vn, NWS_vs_independent=round(sum(sp) / n, 4),
                       worst_core_vs_independent=round(min(sp), 4),
                       HS_vs_independent=round(n / sum(1 / x for x in sp), 4))
            if ref:
                sb = [x["ipc"] / y["ipc"] for x, y in zip(b["cores"], ref["cores"])]
                sv = [x["ipc"] / y["ipc"] for x, y in zip(v["cores"], ref["cores"])]
                row.update(independent_NWS_vs_noPF=round(sum(sb) / n, 4), coord_NWS_vs_noPF=round(sum(sv) / n, 4),
                           independent_worst_vs_noPF=round(min(sb), 4), coord_worst_vs_noPF=round(min(sv), 4),
                           casualties_independent=sum(x < 0.98 for x in sb), casualties_coord=sum(x < 0.98 for x in sv))
            if s == "4core" and (p, m) in oracle:
                ob = oracle[(p, m)]
                row["oracle_best_NWS"] = round(ob, 4)
                row["share_of_oracle_headroom"] = round((row["NWS_vs_independent"] - 1) / (ob - 1), 3) if ob > 1.005 else None
            dr = lambda x: sum(c["miss"] - c["wbmiss"] for c in x["cores"]) / (sum(c["ins"] for c in x["cores"]) / 1000)
            row.update(DRAM_RPKI_independent=round(dr(b), 3), DRAM_RPKI_coord=round(dr(v), 3),
                       DRAM_busy50_independent=b["busy50"], DRAM_busy50_coord=v["busy50"], trials=v["vsum"])
            mixrows.append(row)
            for i in range(n):
                corerows.append(dict(setup=s, prefetcher=p, mix=m, variant=vn, core=i, speedup_vs_independent=round(sp[i], 4),
                                     independent_vs_noPF=round(b["cores"][i]["ipc"] / ref["cores"][i]["ipc"], 4) if ref else None,
                                     coord_vs_noPF=round(v["cores"][i]["ipc"] / ref["cores"][i]["ipc"], 4) if ref else None,
                                     final_level=v["cores"][i]["final_level"],
                                     epochs_at_level_0_1_2_3=" ".join(map(str, v["cores"][i]["lvl_epochs"]))))
    for (s, p, vn) in sorted({(r["setup"], r["prefetcher"], r["variant"]) for r in mixrows}):
        R = [r for r in mixrows if (r["setup"], r["prefetcher"], r["variant"]) == (s, p, vn)]
        g = math.exp(st.mean(math.log(r["NWS_vs_independent"]) for r in R))
        sh = [r["share_of_oracle_headroom"] for r in R if r.get("share_of_oracle_headroom") is not None]
        summ.append(dict(setup=s, prefetcher=p, variant=vn, mixes=len(R), geomean_NWS_vs_independent=round(g, 4),
                         mixes_better_1pct=sum(r["NWS_vs_independent"] >= 1.01 for r in R),
                         mixes_worse_1pct=sum(r["NWS_vs_independent"] <= 0.99 for r in R),
                         mean_worst_core_vs_independent=round(st.mean(r["worst_core_vs_independent"] for r in R), 4),
                         casualties_independent=sum(r.get("casualties_independent", 0) for r in R),
                         casualties_coord=sum(r.get("casualties_coord", 0) for r in R),
                         mean_worst_vs_noPF_independent=round(st.mean(r["independent_worst_vs_noPF"] for r in R if "independent_worst_vs_noPF" in r), 4) if any("independent_worst_vs_noPF" in r for r in R) else None,
                         mean_worst_vs_noPF_coord=round(st.mean(r["coord_worst_vs_noPF"] for r in R if "coord_worst_vs_noPF" in r), 4) if any("coord_worst_vs_noPF" in r for r in R) else None,
                         median_share_of_oracle_headroom=round(st.median(sh), 3) if sh else None))
    out.mkdir(parents=True, exist_ok=True)
    for name, data in (("summary", summ), ("mix", mixrows), ("core", corerows)):
        if data:
            keys = list(dict.fromkeys(k for d in data for k in d))
            with open(out / ("eval_%s.csv" % name), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=keys, restval="")
                w.writeheader()
                w.writerows(data)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        first = True
        for name, data, note in (("Summary", summ, "Controller vs independent UCP + prefetcher (NWS = mean per-core IPC ratio); "
                                  "casualty = core > 2% slower than plain LRU without prefetching"),
                                 ("Per mix", mixrows, "One row per mix, prefetcher and controller variant"),
                                 ("Per core", corerows, "Per core: speedups and the prefetch levels the controller used")):
            ws = wb.active if first else wb.create_sheet(name)
            ws.title = name
            first = False
            ws.append([note])
            ws["A1"].font = Font(bold=True)
            if data:
                hdr = list(dict.fromkeys(k for d in data for k in d))
                ws.append(hdr)
                for c in ws[2]:
                    c.font = Font(bold=True)
                for d in data:
                    ws.append([d.get(k) for k in hdr])
        wb.save(out / "eval_report.xlsx")
    except ImportError:
        pass
    for r in summ:
        print("%(setup)s %(prefetcher)s %(variant)s: %(mixes)d mixes, geomean NWS vs independent %(geomean_NWS_vs_independent).4f, "
              "better/worse by 1%%: %(mixes_better_1pct)d/%(mixes_worse_1pct)d, casualties %(casualties_independent)d -> "
              "%(casualties_coord)d, oracle headroom captured (median) %(median_share_of_oracle_headroom)s" % r)
    say("report: %s" % (out / "eval_report.xlsx"))


if __name__ == "__main__":
    main()
