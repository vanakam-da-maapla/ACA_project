#!/usr/bin/env python3
"""
run_alone_ipc.py -- single-core ("alone") IPC of every trace used in the 4-core mixes, for weighted speedup.

Why: weighted speedup, harmonic speedup and max slowdown divide each core's IPC in a mix by the IPC the same trace
reaches ALONE on the same machine (same 2 MB / 2048-set x 16-way LLC, 50M warm-up + 200M simulated instructions).

What it does (one command, your repository is not modified):
  1. finds the traces: every  *.champsimtrace.xz  named in  <results-root>/*/mix*/command.txt  (or --traces a b c ...)
  2. builds 1-core ChampSim binaries in a THROW-AWAY COPY of the sources (LLC fixed to 2048 sets x 16 ways, SRRIP):
        none : no prefetcher            (the usual denominator; Excel 'Degree 0')
        ipcp : IPCP at L1D + L2C        (optional; Excel 'Degree 6')
  3. runs each trace alone, --jobs at a time, under nice, logs in  <out-dir>/<config>/<trace>.txt  (finished logs are skipped)
  4. writes  <out-dir>/alone_ipc.xlsx  ('Raw Data' tab: Trace, Way, Degree, IPC -- the format the make_ucp_vs_*_excel
     scripts read with  --alone-xlsx ... --alone-way 16 --alone-degree 0)  and  alone_ipc.csv

Usage (from the ChampSim repo root of the IPCP tree):
    python3 scripts/run_alone_ipc.py --jobs 6                         # no-prefetcher denominators only
    python3 scripts/run_alone_ipc.py --jobs 6 --configs none,ipcp     # also alone-with-IPCP denominators
    python3 scripts/run_alone_ipc.py --dry-run                        # list traces and commands, run nothing
Re-running is safe: finished traces are skipped.  Do not start two copies on the same --out-dir.
Only the Python standard library is needed (openpyxl for the .xlsx).
"""
import argparse
import csv
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SRC_ITEMS = ("Makefile", "build_champsim.sh", "src", "inc", "branch", "prefetcher", "replacement")
CONFIGS = {  # name -> (l1d prefetcher, l2c prefetcher, Excel 'Degree' code)
    "none": ("no", "no", 0),
    "ipcp": ("ipcp_isca2020", "ipcp_isca2020", 6),
}
procs, procs_lock, stop = set(), threading.Lock(), threading.Event()


def find_traces(results_root):
    names = set()
    for f in sorted(Path(results_root).glob("*/mix*/command.txt")):
        names.update(re.findall(r"([^\s/]+)\.champsimtrace\.xz", f.read_text(errors="replace")))
    return sorted(names)


def build(root, cfg, bp, sets, ways, build_jobs, bindir):
    l1d, l2c, _ = CONFIGS[cfg]
    out = bindir / ("%s-no-%s-%s-no-srrip-1core-llc%ds%dw" % (bp, l1d, l2c, sets, ways))
    if out.is_file():
        print("[BUILD %s] reusing %s" % (cfg, out), flush=True)
        return out
    for item in SRC_ITEMS:
        if not (root / item).exists():
            sys.exit("[ERROR] %s/%s not found -- run from the ChampSim repository root or pass --root" % (root, item))
    tmp = Path(tempfile.mkdtemp(prefix="champsim_alone_"))
    print("[BUILD %s] compiling in a temporary copy %s (your repository is not modified)" % (cfg, tmp), flush=True)
    try:
        for item in SRC_ITEMS:
            s, d = root / item, tmp / item
            if s.is_dir():
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("*.o", "*.bak", "*.d"))
            else:
                shutil.copy2(s, d)
        ch = tmp / "inc/cache.h"
        txt = ch.read_text()
        txt, n1 = re.subn(r"^#define\s+LLC_SET\b.*$", "#define LLC_SET %d" % sets, txt, flags=re.M)
        txt, n2 = re.subn(r"^#define\s+LLC_WAY\b.*$", "#define LLC_WAY %d" % ways, txt, flags=re.M)
        if n1 != 1 or n2 != 1:
            sys.exit("[ERROR] could not find single '#define LLC_SET/LLC_WAY' lines in inc/cache.h")
        ch.write_text(txt)
        env = dict(os.environ, TERM=os.environ.get("TERM", "xterm"), MAKEFLAGS="-j%d" % max(1, build_jobs))
        log = bindir / ("build_%s.log" % cfg)
        bindir.mkdir(parents=True, exist_ok=True)
        with open(log, "w") as lf:
            rc = subprocess.call(["./build_champsim.sh", bp, "no", l1d, l2c, "no", "srrip", "1"], cwd=str(tmp), env=env,
                                 stdout=lf, stderr=subprocess.STDOUT)
        built = tmp / "bin" / ("%s-no-%s-%s-no-srrip-1core" % (bp, l1d, l2c))
        if rc != 0 or not built.is_file():
            print("".join(log.read_text().splitlines(True)[-25:]))
            sys.exit("[ERROR] build failed (see %s)" % log)
        shutil.copy2(built, out)
        out.chmod(0o755)
        print("[BUILD %s] done: %s" % (cfg, out), flush=True)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parse_ipc(path):
    """(IPC, instructions, cycles) of CPU 0 from a finished single-core log, or None."""
    try:
        t = Path(path).read_text(errors="replace")
    except OSError:
        return None
    m = re.findall(r"Finished CPU 0 instructions:\s*(\d+)\s+cycles:\s*(\d+)", t)
    if not m:
        m = re.findall(r"CPU 0 cumulative IPC:\s*[0-9.eE+-]+\s+instructions:\s*(\d+)\s+cycles:\s*(\d+)", t)
    if not m or "ChampSim completed all CPUs" not in t:
        return None
    ins, cyc = int(m[-1][0]), int(m[-1][1])
    return (ins / cyc, ins, cyc) if cyc else None


def run_one(binary, root, trace_file, log, warm, sim, extra):
    if stop.is_set():
        return "cancelled"
    log.parent.mkdir(parents=True, exist_ok=True)
    tmp = log.with_suffix(".running")
    cmd = ["nice", "-n", "19", str(binary), "-warmup_instructions", str(warm), "-simulation_instructions", str(sim)] \
        + extra + ["-traces", str(trace_file)]
    t0 = time.time()
    with open(tmp, "w") as lf:
        p = subprocess.Popen(cmd, cwd=str(root), stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
        with procs_lock:
            procs.add(p)
        rc = p.wait()
        with procs_lock:
            procs.discard(p)
    if stop.is_set():
        tmp.unlink(missing_ok=True)
        return "cancelled"
    if rc == 0 and parse_ipc(tmp):
        tmp.rename(log)
        return "ok %.0f min" % ((time.time() - t0) / 60)
    shutil.move(str(tmp), str(log.with_suffix(".failed")))
    return "FAILED (rc=%s, see %s)" % (rc, log.with_suffix(".failed"))


def kill_all(*_):
    stop.set()
    with procs_lock:
        for p in list(procs):
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="single-core alone IPC of the mix traces (denominator of weighted speedup)")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="ChampSim repository root (default: current directory)")
    ap.add_argument("--results-root", type=Path, default=None, help="folder with the 4-core results (default <root>/results_4core_mixes_ipcp)")
    ap.add_argument("--traces", nargs="+", default=None, help="trace names (without .champsimtrace.xz); default: all traces found in the results")
    ap.add_argument("--trace-dir", type=Path, default=None, help="folder with *.champsimtrace.xz (default <root>/dpc3_traces)")
    ap.add_argument("--configs", default="none", help="comma list of: none, ipcp  (default none)")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--warmup", type=float, default=50, help="warm-up instructions, millions (same as the mixes)")
    ap.add_argument("--sim", type=float, default=200, help="simulated instructions, millions (same as the mixes)")
    ap.add_argument("--llc-sets", type=int, default=2048)
    ap.add_argument("--llc-ways", type=int, default=16)
    ap.add_argument("--bp", default="perceptron")
    ap.add_argument("--build-jobs", type=int, default=8)
    ap.add_argument("--out-dir", type=Path, default=None, help="default <root>/alone_ipc")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = a.root.resolve()
    res = a.results_root or root / "results_4core_mixes_ipcp"
    trace_dir = a.trace_dir or root / "dpc3_traces"
    out_dir = a.out_dir or root / "alone_ipc"
    cfgs = [c.strip() for c in a.configs.split(",") if c.strip()]
    for c in cfgs:
        if c not in CONFIGS:
            sys.exit("[ERROR] unknown config %r (use none and/or ipcp)" % c)
    traces = a.traces or find_traces(res)
    if not traces:
        sys.exit("[ERROR] no traces found in %s/*/mix*/command.txt (pass --traces)" % res)
    missing = [t for t in traces if not (trace_dir / (t + ".champsimtrace.xz")).is_file()]
    if missing:
        sys.exit("[ERROR] %d trace(s) missing in %s: %s" % (len(missing), trace_dir, ", ".join(missing[:5])))
    warm, sim = int(a.warmup * 1e6), int(a.sim * 1e6)
    print("%d traces x %d config(s) = %d single-core simulations (warm-up %gM + %gM), %d at a time"
          % (len(traces), len(cfgs), len(traces) * len(cfgs), a.warmup, a.sim, a.jobs), flush=True)
    if a.dry_run:
        for c in cfgs:
            for t in traces:
                print("[%s] %s" % (c, t))
        return
    signal.signal(signal.SIGINT, kill_all)
    signal.signal(signal.SIGTERM, kill_all)
    bindir = root / "bin"
    binaries = {c: build(root, c, a.bp, a.llc_sets, a.llc_ways, a.build_jobs, bindir) for c in cfgs}
    jobs = []
    for t in traces:                       # interleave configs per trace
        for c in cfgs:
            log = out_dir / c / (t + ".txt")
            if parse_ipc(log):
                continue
            jobs.append((c, t, log))
    print("%d to run, %d already done" % (len(jobs), len(traces) * len(cfgs) - len(jobs)), flush=True)

    def work(j):
        c, t, log = j
        print("[START] %s %s" % (c, t), flush=True)
        r = run_one(binaries[c], root, trace_dir / (t + ".champsimtrace.xz"), log, warm, sim, [])
        print("[%s] %s %s" % (r.split()[0].upper(), c, t) + ("" if r.startswith("ok") else "  " + r), flush=True)
        return r
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        list(ex.map(work, jobs))
    if stop.is_set():
        print("[INTERRUPTED] finished logs are kept; re-run the same command to continue")
        return
    rows = []
    for t in traces:
        for c in cfgs:
            r = parse_ipc(out_dir / c / (t + ".txt"))
            if r:
                rows.append((t, a.llc_ways, CONFIGS[c][2], r[0], c))
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "alone_ipc.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Trace", "Way", "Degree", "IPC", "Config"])
        w.writerows(rows)
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Raw Data"
        ws.append(["Trace", "Way", "Degree", "IPC", "Config"])
        for r in rows:
            ws.append(list(r))
        wb.save(out_dir / "alone_ipc.xlsx")
        print("[DONE] %d values -> %s and alone_ipc.csv" % (len(rows), out_dir / "alone_ipc.xlsx"))
    except ImportError:
        print("[DONE] %d values -> %s (openpyxl missing: send the csv)" % (len(rows), out_dir / "alone_ipc.csv"))
    bad = len(traces) * len(cfgs) - len(rows)
    if bad:
        print("[WARN] %d simulation(s) have no result; look for *.failed files under %s" % (bad, out_dir))


if __name__ == "__main__":
    main()
