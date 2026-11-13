#!/usr/bin/env python3
"""
plot_8core_report.py -- publication-style plots of the 8-core scale-up workbook (8core_report.xlsx)
==================================================================================================
Reads the workbook written by 4to8core_scaleup_exp.py --report-only and draws, with full axis labels and a
glossary of every abbreviation on each figure:
  fig1_throughput.png        throughput of each configuration at normal vs low DRAM bandwidth
  fig2_casualties.png        cores slowed down (> 2%) and cores hurt by running prefetching + partitioning together
  fig3_fairness.png          maximum slowdown and fairness (min / max per-core speedup over its alone run)
  fig4_percore_curves.png    every core's speedup, sorted (the tails averages hide)
  fig5_interaction.png       per-core interaction of prefetching and partitioning
  fig6_dram.png              DRAM traffic and DRAM busy time
  fig7_by_class.png          who gets hurt: casualty share per workload class
  fig8_per_mix.png           every mix, both bandwidths
  fig9_oracle.png            best fixed per-core configuration (oracle) vs the independent run, per mix (if present)
  all_figures.pdf            all of the above in one file

Usage (repository root):
    python3 scripts/plot_8core_report.py                                   # scaleup_8core_results/8core_report.xlsx
    python3 scripts/plot_8core_report.py --xlsx some.xlsx --out plots_dir
Needs openpyxl and matplotlib.
"""
import argparse
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

import matplotlib
import matplotlib.ticker
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.backends.backend_pdf import PdfPages  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

GLOSSARY = ("Abbreviations:  PF = hardware prefetcher (Pythia or IPCP at L1D + L2C)   UCP = Utility-based Cache Partitioning "
            "of the shared LLC   LRU = plain least-recently-used LLC, no partitioning   LLC = last-level cache   "
            "NWS = normalised weighted speedup = mean over the 8 cores of IPC / IPC on plain LRU without PF   "
            "IPC = instructions per cycle   MT/s = DRAM mega-transfers per second (1200 = low bandwidth, 2.4 GB/s per core; "
            "2400 = normal, 4.8 GB/s per core)   casualty = core > 2% slower than on plain LRU without PF   "
            "HC / LC = high / low cache sensitivity   HP / LP = high / low prefetch sensitivity   "
            "MPKI / RPKI = misses / DRAM reads per kilo-instruction")
CFG = {"none": "No PF, no partitioning (plain LRU)", "part": "Partitioning only (UCP, no PF)",
       "pf": "Prefetching only (PF on plain LRU)", "both": "Prefetching + partitioning (PF + UCP, independent)",
       "both_umon": "PF + UCP whose monitor ignores prefetches"}
CFG_ORDER = ["none", "part", "pf", "both"]
BW = {1200: "Low bandwidth (1200 MT/s, 2.4 GB/s per core)", 2400: "Normal bandwidth (2400 MT/s, 4.8 GB/s per core)"}
CLS = {"HCHP": "HCHP (high cache sensitivity, high prefetch sensitivity)",
       "HCLP": "HCLP (high cache sensitivity, low prefetch sensitivity)",
       "LCHP": "LCHP (low cache sensitivity, high prefetch sensitivity)",
       "LCLP": "LCLP (low cache sensitivity, low prefetch sensitivity)"}
SHORT = {"none": "No PF, no partitioning\n(plain LRU)", "part": "Partitioning\nonly (UCP)", "pf": "Prefetching\nonly", "both": "Prefetching +\npartitioning"}
MTYPE = {"homogeneous 8": "1 class\n8", "two-class 4+4": "2 classes\n4+4", "two-class 6+2": "2 classes\n6+2",
         "three-class 4+2+2": "3 classes\n4+2+2", "four-class 2+2+2+2": "4 classes\n2+2+2+2"}
PF_NAME = {"pythia": "Pythia", "ipcp": "IPCP", "-": "no prefetcher"}
COL = {"none": "#7f7f7f", "part": "#e0a030", "pf": "#2a7ab9", "both": "#2e8b57", "both_umon": "#9467bd"}


def rows(wb, name):
    if name not in wb.sheetnames:
        return []
    it = list(wb[name].iter_rows(values_only=True))
    if len(it) < 3:
        return []
    h = it[1]
    return [dict(zip(h, r)) for r in it[2:] if r and r[0] is not None]


def gmean(v):
    v = [x for x in v if x]
    return math.exp(st.mean(math.log(x) for x in v)) if v else float("nan")


def finish(fig, pdf, out, name, title):
    fig.suptitle(title, fontsize=12, fontweight="bold")
    fig.text(0.01, 0.005, GLOSSARY, fontsize=6.3, wrap=True, va="bottom", ha="left", color="#333333")
    fig.tight_layout(rect=(0, 0.09, 1, 0.95))
    fig.savefig(out / name, dpi=170)
    pdf.savefig(fig)
    plt.close(fig)
    print("  wrote", out / name)


def main():
    here = Path(__file__).resolve().parent
    root = here.parent if here.name == "scripts" else Path.cwd()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--xlsx", type=Path, default=root / "scaleup_8core_results" / "8core_report.xlsx")
    ap.add_argument("--out", type=Path, default=root / "scaleup_8core_results" / "plots")
    a = ap.parse_args()
    wb = load_workbook(a.xlsx, read_only=True)
    M, C, I, K = rows(wb, "Per mix"), rows(wb, "Per core"), rows(wb, "Interaction"), rows(wb, "By class")
    if not M:
        raise SystemExit("[ERROR] %s has no 'Per mix' rows -- build it with 4to8core_scaleup_exp.py --report-only "
                         "(without --oracle-only)" % a.xlsx)
    a.out.mkdir(parents=True, exist_ok=True)
    pdf = PdfPages(a.out / "all_figures.pdf")
    pfs = [p for p in ("pythia", "ipcp") if any(d["prefetcher"] == p for d in M)]
    fact = [d for d in M if d["tier"] in (1, 2)]
    mixes_both = sorted({d["mix"] for d in fact if d["bw"] == 1200} & {d["mix"] for d in fact if d["bw"] == 2400})

    def sel(bw, cfg, p):
        return [d for d in fact if d["bw"] == bw and d["config"] == cfg and d["prefetcher"] in (p, "-")
                and d["mix"] in mixes_both]

    # ---- fig1 throughput
    fig, axs = plt.subplots(1, len(pfs), figsize=(13, 5.2), squeeze=False)
    for ax, p in zip(axs[0], pfs):
        x = range(len(CFG_ORDER))
        for k, bw in enumerate((2400, 1200)):
            vals = [gmean(d["NWS_vs_noPF_noPart"] for d in sel(bw, c, p)) for c in CFG_ORDER]
            bars = ax.bar([i + (k - 0.5) * 0.38 for i in x], vals, 0.38, label=BW[bw],
                          color="#9ecae1" if bw == 2400 else "#08519c")
            for b, v in zip(bars, vals):
                ax.text(b.get_x() + b.get_width() / 2, v + 0.005, "%.3f" % v, ha="center", fontsize=7)
        ax.axhline(1.0, color="k", lw=0.7)
        ax.set_xticks(list(x))
        ax.set_xticklabels([SHORT[c] for c in CFG_ORDER], fontsize=7)
        ax.set_ylabel("Throughput: geometric-mean NWS\n(relative to plain LRU without PF = 1.0)", fontsize=8)
        ax.set_title("%s (%d mixes, same mixes at both bandwidths)" % (PF_NAME[p], len(mixes_both)), fontsize=9)
        ax.legend(fontsize=7)
    finish(fig, pdf, a.out, "fig1_throughput.png",
           "Fig. 1  Average throughput hardly changes with partitioning, and cutting DRAM bandwidth shrinks the prefetch gain")

    # ---- fig2 casualties + conflict
    fig, axs = plt.subplots(1, 2, figsize=(13, 5.2))
    labels, cas, conflict = [], {2400: [], 1200: []}, {2400: [], 1200: []}
    for p in pfs:
        for c in ("part", "pf", "both"):
            labels.append(SHORT[c] if c == "part" else "%s\n%s" % (PF_NAME[p], SHORT[c]))
            for bw in (2400, 1200):
                cas[bw].append(sum(d["casualties"] or 0 for d in sel(bw, c, p)))
    for p in pfs:
        for bw in (2400, 1200):
            X = [d for d in I if d["prefetcher"] == p and d["bw"] == bw and d["mix"] in mixes_both]
            conflict[bw].append(100.0 * sum(bool(d["both_worse_than_best_single"]) for d in X) / max(1, len(X)))
    x = range(len(labels))
    for k, bw in enumerate((2400, 1200)):
        axs[0].bar([i + (k - 0.5) * 0.38 for i in x], cas[bw], 0.38, label=BW[bw], color="#fdae6b" if bw == 2400 else "#d94801")
    axs[0].set_xticks(list(x))
    axs[0].set_xticklabels(labels, fontsize=7)
    axs[0].set_ylabel("Casualty cores (> 2%% slower than plain LRU without PF)\nout of %d cores" % (8 * len(mixes_both)), fontsize=8)
    axs[0].set_title("Cores slowed down by each mechanism", fontsize=9)
    axs[0].legend(fontsize=7)
    x2 = range(len(pfs))
    for k, bw in enumerate((2400, 1200)):
        b = axs[1].bar([i + (k - 0.5) * 0.38 for i in x2], conflict[bw], 0.38, label=BW[bw],
                       color="#fdae6b" if bw == 2400 else "#d94801")
        for bb, v in zip(b, conflict[bw]):
            axs[1].text(bb.get_x() + bb.get_width() / 2, v + 0.5, "%.0f%%" % v, ha="center", fontsize=8)
    axs[1].set_xticks(list(x2))
    axs[1].set_xticklabels([PF_NAME[p] for p in pfs])
    axs[1].set_ylabel("Share of cores for which PF + UCP is > 2% worse\nthan the better of PF only / UCP only (%)", fontsize=8)
    axs[1].set_title("Conflict: running both mechanisms is worse than either alone", fontsize=9)
    axs[1].legend(fontsize=7)
    finish(fig, pdf, a.out, "fig2_casualties.png",
           "Fig. 2  Low bandwidth multiplies the cores that independent prefetching and partitioning hurt")

    # ---- fig3 fairness
    fig, axs = plt.subplots(1, 2, figsize=(13, 5.2))
    for ax, key, ylab in ((axs[0], "max_slowdown", "Maximum slowdown (mean over mixes)\n= max over cores of alone IPC / shared IPC  (lower is better)"),
                          (axs[1], "fairness_min_over_max", "Fairness (mean over mixes)\n= min / max of per-core speedup over alone  (1 = perfectly fair)")):
        labs, vals = [], {2400: [], 1200: []}
        for p in pfs:
            for c in CFG_ORDER:
                if c in ("none", "part") and p != pfs[0]:
                    continue
                labs.append(SHORT[c] if c in ("none", "part") else "%s\n%s" % (PF_NAME[p], SHORT[c]))
                for bw in (2400, 1200):
                    v = [d[key] for d in sel(bw, c, p) if d.get(key)]
                    vals[bw].append(st.mean(v) if v else float("nan"))
        x = range(len(labs))
        for k, bw in enumerate((2400, 1200)):
            ax.bar([i + (k - 0.5) * 0.38 for i in x], vals[bw], 0.38, label=BW[bw], color="#bcbddc" if bw == 2400 else "#54278f")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labs, fontsize=7)
        ax.set_ylabel(ylab, fontsize=8)
        ax.legend(fontsize=7)
    axs[0].set_title("Worst-off core", fontsize=9)
    axs[1].set_title("Balance between cores", fontsize=9)
    finish(fig, pdf, a.out, "fig3_fairness.png", "Fig. 3  Fairness collapses under bandwidth pressure when prefetching is on")

    # ---- fig4 per-core sorted curves
    fig, axs = plt.subplots(1, len(pfs), figsize=(13, 5.2), squeeze=False)
    for ax, p in zip(axs[0], pfs):
        for c in ("part", "pf", "both"):
            for bw, ls in ((2400, ":"), (1200, "-")):
                v = sorted(d["speedup_vs_noPF_noPart"] for d in C if d["bw"] == bw and d["config"] == c
                           and d["prefetcher"] in (p, "-") and d["mix"] in mixes_both and d["speedup_vs_noPF_noPart"])
                if v:
                    ax.plot([100.0 * i / (len(v) - 1) for i in range(len(v))], v, ls=ls, lw=1.4, color=COL[c],
                            label="%s, %s" % (CFG[c].split(" (")[0], "low BW" if bw == 1200 else "normal BW"))
        ax.axhline(1.0, color="k", lw=0.7)
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(matplotlib.ticker.FixedLocator([0.25, 0.33, 0.5, 0.67, 1, 1.5, 2, 3, 4]))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: "%gx" % v))
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.set_xlabel("Cores sorted by speedup (percentile of all %d cores)" % (8 * len(mixes_both)), fontsize=8)
        ax.set_ylabel("Per-core speedup vs plain LRU without PF (log scale)", fontsize=8)
        ax.set_title(PF_NAME[p], fontsize=9)
        ax.legend(fontsize=6.5)
    finish(fig, pdf, a.out, "fig4_percore_curves.png",
           "Fig. 4  Averages hide the left tail: 20-30% of cores lose under low bandwidth (solid) vs normal (dotted)")

    # ---- fig5 interaction
    fig, axs = plt.subplots(1, 2, figsize=(13, 5.2))
    for p in pfs:
        for bw, ls in ((2400, ":"), (1200, "-")):
            v = sorted(d["interaction"] for d in I if d["prefetcher"] == p and d["bw"] == bw and d["mix"] in mixes_both)
            if v:
                axs[0].plot([100.0 * i / (len(v) - 1) for i in range(len(v))], v, ls=ls,
                            color="#2a7ab9" if p == "pythia" else "#e0a030",
                            label="%s, %s" % (PF_NAME[p], "low BW" if bw == 1200 else "normal BW"))
    axs[0].axhline(1.0, color="k", lw=0.7)
    axs[0].set_xlabel("Cores sorted by interaction (percentile)", fontsize=8)
    axs[0].set_ylabel("Interaction = speedup(PF + UCP) / (speedup(PF only) x speedup(UCP only))\n< 1: the two mechanisms hurt each other", fontsize=8)
    axs[0].set_title("Per-core interaction", fontsize=9)
    axs[0].legend(fontsize=7)
    Q = rows(wb, "PF->partition")
    for p in pfs:
        v = [d["delta_ways"] for d in Q if d["prefetcher"] == p and str(d["monitor"]).startswith("UCP")]
        if v:
            axs[1].hist(v, bins=21, range=(-3, 3), alpha=0.6, label="%s (%d cores)" % (PF_NAME[p], len(v)))
    axs[1].set_xlabel("Change of a core's UCP way allocation when the prefetcher is switched on (ways, run average)", fontsize=8)
    axs[1].set_ylabel("Number of cores", fontsize=8)
    axs[1].set_title("UCP barely reacts to prefetching: the harm travels through DRAM bandwidth", fontsize=9)
    axs[1].legend(fontsize=7)
    finish(fig, pdf, a.out, "fig5_interaction.png", "Fig. 5  How prefetching and partitioning interact")

    # ---- fig6 DRAM
    fig, axs = plt.subplots(1, 2, figsize=(13, 5.2))
    for ax, key, ylab in ((axs[0], "DRAM_reads_PKI", "DRAM reads per kilo-instruction (RPKI), mean over mixes"),
                          (axs[1], "DRAM_busy_gt50", "Share of time DRAM is more than 50% busy")):
        labs, vals = [], {2400: [], 1200: []}
        for p in pfs:
            for c in CFG_ORDER:
                if c in ("none", "part") and p != pfs[0]:
                    continue
                labs.append(SHORT[c] if c in ("none", "part") else "%s\n%s" % (PF_NAME[p], SHORT[c]))
                for bw in (2400, 1200):
                    v = [d[key] for d in sel(bw, c, p) if d.get(key) is not None]
                    vals[bw].append(st.mean(v) if v else float("nan"))
        x = range(len(labs))
        for k, bw in enumerate((2400, 1200)):
            ax.bar([i + (k - 0.5) * 0.38 for i in x], vals[bw], 0.38, label=BW[bw], color="#a1d99b" if bw == 2400 else "#006d2c")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labs, fontsize=7)
        ax.set_ylabel(ylab, fontsize=8)
        ax.legend(fontsize=7)
    finish(fig, pdf, a.out, "fig6_dram.png", "Fig. 6  Prefetching multiplies DRAM traffic and keeps the DRAM busy")

    # ---- fig7 by class
    fig, axs = plt.subplots(1, len(pfs), figsize=(13, 5.2), squeeze=False)
    for ax, p in zip(axs[0], pfs):
        x = range(4)
        k = 0
        for c in ("pf", "both"):
            for bw in (2400, 1200):
                v = []
                for cl in CLS:
                    r = [d for d in K if d["prefetcher"] == p and d["config"] == c and d["bw"] == bw and d["cls"] == cl]
                    v.append(100.0 * r[0]["casualty_share"] if r and r[0]["casualty_share"] is not None else 0)
                ax.bar([i + (k - 1.5) * 0.2 for i in x], v, 0.2,
                       label="%s, %s" % (CFG[c].split(" (")[0], "low BW" if bw == 1200 else "normal BW"),
                       color={("pf", 2400): "#9ecae1", ("pf", 1200): "#2171b5", ("both", 2400): "#a1d99b",
                              ("both", 1200): "#238b45"}[(c, bw)])
                k += 1
        ax.set_xticks(list(x))
        ax.set_xticklabels([c + "\n" + CLS[c].split("(", 1)[1].rstrip(")").replace(", ", ",\n") for c in CLS], fontsize=7)
        ax.set_ylabel("Casualty cores in the class (%)", fontsize=8)
        ax.set_title(PF_NAME[p], fontsize=9)
        ax.legend(fontsize=6.5)
    finish(fig, pdf, a.out, "fig7_by_class.png",
           "Fig. 7  The victims are cache-sensitive cores (HC), mostly those with little prefetch benefit (HCLP)")

    # ---- fig8 per mix
    fig, axs = plt.subplots(len(pfs), 1, figsize=(13, 3.6 * len(pfs)), squeeze=False)
    for ax, p in zip(axs[:, 0], pfs):
        x = range(len(mixes_both))
        for k, (bw, c) in enumerate(((2400, "both"), (1200, "both"))):
            v = []
            for m in mixes_both:
                r = [d for d in fact if d["bw"] == bw and d["config"] == c and d["prefetcher"] == p and d["mix"] == m]
                v.append(r[0]["NWS_vs_noPF_noPart"] if r else float("nan"))
            ax.bar([i + (k - 0.5) * 0.38 for i in x], v, 0.38, label="PF + UCP, " + BW[bw],
                   color="#9ecae1" if bw == 2400 else "#08519c")
        for i, m in enumerate(mixes_both):
            r = [d for d in fact if d["bw"] == 1200 and d["config"] == "both" and d["prefetcher"] == p and d["mix"] == m]
            if r and r[0]["worst_core_vs_noPF_noPart"]:
                ax.plot(i + 0.19, r[0]["worst_core_vs_noPF_noPart"], "v", color="#d94801", ms=5,
                        label="slowest core, low BW" if i == 0 else None)
        ax.axhline(1.0, color="k", lw=0.7)
        ax.set_xticks(list(x))
        ax.set_xticklabels(["mix%02d\n%s" % (m, MTYPE.get(str(next(d["mix_type"] for d in fact if d["mix"] == m)), ""))
                            for m in mixes_both], fontsize=6)
        ax.set_xlabel("Mix (number of workload classes and how many of the 8 cores run each class)", fontsize=8)
        ax.set_ylabel("NWS / slowest core\nvs plain LRU without PF", fontsize=8)
        ax.set_title(PF_NAME[p], fontsize=9)
        ax.legend(fontsize=6.5, ncol=3)
    finish(fig, pdf, a.out, "fig8_per_mix.png", "Fig. 8  Every mix: average gain (bars) vs the slowest core (triangles)")

    # ---- fig9 oracle
    O = list(wb["Oracle"].iter_rows(values_only=True)) if "Oracle" in wb.sheetnames else []
    per = []
    for r in O:
        if r and r[0] in PF_NAME and isinstance(r[1], int) and isinstance(r[2], str) and isinstance(r[5], (int, float)):
            per.append(r)
    if per:
        fig, ax = plt.subplots(figsize=(13, 5.2))
        x = range(len(per))
        ax.bar([i - 0.2 for i in x], [r[5] for r in per], 0.4, color="#2e8b57",
               label="Best fixed per-core configuration (oracle), throughput vs independent PF + UCP")
        ax.bar([i + 0.2 for i in x], [r[6] for r in per], 0.4, color="#fd8d3c",
               label="Slowest core of that configuration vs independent PF + UCP")
        ax.axhline(1.0, color="k", lw=0.7)
        ax.set_xticks(list(x))
        def cfgtxt(c):
            w, _, d = str(c).partition("_d")
            w = w[1:]
            return "ways: %s\nPF caps: %s" % ("UCP adaptive" if w == "ucp" else w, d)
        ax.set_xticklabels(["%s mix%02d\n%s\n%s" % (PF_NAME[r[0]], r[1], MTYPE.get(r[2], r[2]).replace("\n", " "), cfgtxt(r[3]))
                            for r in per], fontsize=6)
        ax.set_ylabel("Relative to the independent run (UCP + native PF) = 1.0", fontsize=8)
        ax.legend(fontsize=7)
        ax.set_xlabel("Best configuration per mix -- ways: LLC ways of the 8 cores (UCP adaptive = UCP decides); PF caps: "
                      "prefetch degree cap of the 8 cores (6 = native prefetcher, 2 = degree 2, 0 = prefetcher off)", fontsize=7)
        finish(fig, pdf, a.out, "fig9_oracle.png",
               "Fig. 9  Room for coordination at low bandwidth: best fixed per-core settings vs independent operation")
    pdf.close()
    print("  wrote", a.out / "all_figures.pdf")


if __name__ == "__main__":
    main()
