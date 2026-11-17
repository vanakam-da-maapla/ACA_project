#!/usr/bin/env python3
"""Regenerate every data figure (figures/*.pdf) and table (tables/*.tex) of the paper from the result workbooks
in data/.  Run from the paper root:  python3 scripts/make_figures.py
When the 8-core v3.3 run finishes, copy its report to data/v33_8core.xlsx and run this again: the 8-core
results table is then filled in automatically."""
import collections
import re
import math
import statistics as st
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import openpyxl  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
D, FIG, TAB = ROOT / "data", ROOT / "figures", ROOT / "tables"
FIG.mkdir(exist_ok=True)
TAB.mkdir(exist_ok=True)

plt.rcParams.update({"font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "legend.fontsize": 7,
                     "xtick.labelsize": 7, "ytick.labelsize": 7, "figure.dpi": 200, "savefig.bbox": "tight",
                     "axes.spines.top": False, "axes.spines.right": False, "pdf.fonttype": 42})
COL = {"vpt_part": "#8c8c8c", "psr": "#c49a6c", "v22": "#d4a017", "v3": "#9ecae1", "v3w": "#6baed6",
       "v32": "#3182bd", "v33": "#08519c"}
NAME = {"vpt_part": "FDT", "psr": "FDT+PSR", "v22": "FDT+Reclaim", "v3": "TSC-T",
        "v3w": "TSC-TP", "v32": "TSC-TPG", "v33": "\\textbf{TSC}"}
PNAME = {"ipcp": "IPCP", "pythia": "Pythia"}
gm = lambda xs: math.exp(st.mean(math.log(x) for x in xs)) if xs else float("nan")


def sheet(path, name):
    R = list(openpyxl.load_workbook(path, read_only=True, data_only=True)[name].iter_rows(values_only=True))
    h = R[1]
    return [dict(zip(h, r)) for r in R[2:] if r and r[0] is not None]


def pct(x, d=1):
    v = 100 * (x - 1)
    if abs(v) < 0.5 * 10 ** (-d):
        return ("%." + str(d) + "f\\%%") % 0.0
    return ("%+." + str(d) + "f\\%%") % v


def write(name, text):
    (TAB / name).write_text(text)
    print("wrote tables/%s" % name)


# ------------------------------------------------------------------ Table: 4-core characterisation (independent)
def char_table():
    rows = []
    for f, pf, a, b, c in (("char_pythia_4core.xlsx", "Pythia", "UCP", "UCP_PYTHIA", "PYTHIA"),
                           ("char_ipcp_4core.xlsx", "IPCP", "UCP", "UCP_IPCP_L1_L2", "IPCP_L1_L2")):
        R = list(openpyxl.load_workbook(D / f, read_only=True, data_only=True)["Summary"].iter_rows(values_only=True))
        get = {}
        for r in R:
            if r and isinstance(r[0], str) and r[1] is not None:
                get[r[0].strip()] = r
        def val(key, i):
            for k, r in get.items():
                if k.startswith(key):
                    return float(r[i])
            return float("nan")
        n = 50 if pf == "Pythia" else 20
        rows.append((pf, n, val("Relative weighted speedup", 3), val("Relative weighted speedup", 2),
                     val("Worst-case core", 3), val("Worst-case core", 2), val("DRAM read requests", 1),
                     val("DRAM read requests", 2), val("Average LLC miss latency", 1), val("Average LLC miss latency", 2)))
    t = ["\\begin{tabular}{lrcccccc}", "\\toprule",
         " & & \\multicolumn{2}{c}{Weighted speedup} & \\multicolumn{2}{c}{Worst core} & DRAM reads & LLC miss lat. \\\\",
         "\\cmidrule(lr){3-4}\\cmidrule(lr){5-6}",
         "Prefetcher & Mixes & PF only & PF+UCP & PF only & PF+UCP & PF+UCP & PF+UCP \\\\", "\\midrule"]
    for pf, n, ws_pf, ws_both, wc_pf, wc_both, d0, d1, l0, l1 in rows:
        t.append("%s & %d & %.3f & %.3f & %.3f & %.3f & %s & %s \\\\" % (pf, n, ws_pf, ws_both, wc_pf, wc_both,
                                                                        pct(d1 / d0, 0), pct(l1 / l0, 0)))
    t += ["\\bottomrule", "\\end{tabular}"]
    write("char_4core.tex", "\n".join(t) + "\n")


# ------------------------------------------------------------------ Table: 8-core scale-up (factorial)
def scaleup_table():
    R = list(openpyxl.load_workbook(D / "scaleup_8core.xlsx", read_only=True, data_only=True)["Summary"]
             .iter_rows(values_only=True))
    hdr_i = next(i for i, r in enumerate(R) if r and r[0] == "bw")
    h = R[hdr_i]
    data = [dict(zip(h, r)) for r in R[hdr_i + 1:] if r and r[0] is not None and str(r[0]).isdigit()]
    lab = {"none": "No PF, no partitioning", "part": "UCP only", "pf": "PF only (LRU)", "both": "PF + UCP"}
    t = ["\\begin{tabular}{llrcccc}", "\\toprule",
         "DRAM & Configuration & PF & Thr. vs no-PF & Max slowdown & Fairness & Casualties \\\\", "\\midrule"]
    last = None
    order = {"none": 0, "part": 1, "pf": 2, "both": 3}
    data.sort(key=lambda r: (-int(r["bw"]), order.get(r["config"], 9), str(r["prefetcher"])))
    for r in data:
        if r["config"] not in lab or (r["config"] not in ("none", "part") and r["prefetcher"] not in ("ipcp", "pythia")):
            continue
        if r["config"] == "both_umon":
            continue
        bw = "%s MT/s" % r["bw"]
        if last and last != bw:
            t.append("\\midrule")
        last = bw
        t.append("%s & %s & %s & %.3f & %.2f & %.2f & %s \\\\" % (bw, lab[r["config"]], PNAME.get(r["prefetcher"], "--"),
                                                                 float(r["geomean_NWS_vs_noPF_noPart"]),
                                                                 float(r["mean_max_slowdown"]), float(r["mean_fairness"]),
                                                                 r["casualty_cores"]))
    t += ["\\bottomrule", "\\end{tabular}"]
    write("scaleup_8core.tex", "\n".join(t) + "\n")


# ------------------------------------------------------------------ Table: oracle headroom 4-core vs 8-core
def oracle_table():
    R = list(openpyxl.load_workbook(D / "oracle_4core.xlsx", read_only=True, data_only=True)["Summary"]
             .iter_rows(values_only=True))
    o4 = {}
    for r in R:                                   # the first row per prefetcher is the summary row
        if r and r[0] in ("pythia", "ipcp") and isinstance(r[1], (int, float)) and r[0] not in o4:
            o4[r[0]] = r
    R8 = list(openpyxl.load_workbook(D / "scaleup_8core.xlsx", read_only=True, data_only=True)["Oracle"]
              .iter_rows(values_only=True))
    o8 = [r for r in R8 if r and r[0] == "ipcp" and isinstance(r[1], (int, float)) and isinstance(r[2], (int, float))]
    G = grid_stats()
    t = ["\\begin{tabular}{lllrccc}", "\\toprule",
         "Search & Setup & PF & Mixes & Mean & Max & $\\geq$+3\\% \\\\", "\\midrule"]
    for pf, ch in (("ipcp", "1 ch."), ("pythia", "2 ch.")):
        mixes, _, best = G[pf]
        t.append("Uniform knobs & 4-core, %s & %s & %d & %s & %s & %d \\\\" % (ch, PNAME[pf], len(mixes), pct(gm(best)),
                 pct(max(best)), sum(x >= 1.03 for x in best)))
    t.append("\\midrule")
    for pf in ("ipcp", "pythia"):
        r = o4[pf]
        t.append("Per-core oracle & 4-core, 9.6\\,GB/s & %s & %d & %s & %s & %d \\\\" % (PNAME[pf], r[1], pct(float(r[2])),
                 pct(float(r[3])), int(r[4])))
    if o8:
        r = o8[0]
        t.append("Per-core oracle & 8-core, 2.4\\,GB/s & IPCP & %d & %s & %s & %s \\\\" % (r[1], pct(float(r[2])),
                 pct(float(r[3])), r[4]))
    t += ["\\bottomrule", "\\end{tabular}"]
    write("oracle.tex", "\n".join(t) + "\n")
    oracle_numbers(o4)


# ------------------------------------------------------------------ Figure: root cause (accuracy vs throttling effect)
def root_cause_figure():
    cores = [r for r in sheet(D / "final_4core.xlsx", "Per core") if r["variant"] == "vpt_part" and r["setup"] == "4core_lowbw"]
    cores += [r for r in sheet(D / "v22_8core.xlsx", "Per core") if r["variant"] == "vpt_part"]
    bins = [(0, .2), (.2, .4), (.4, .6), (.6, .8), (.8, 1.01)]
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.3))
    for ax, pf in zip(axes, ("ipcp", "pythia")):
        sp, thr, ben, n = [], [], [], []
        for lo, hi in bins:
            sel = []
            for r in cores:
                if r["prefetcher"] != pf:
                    continue
                u, x = r["pf_useful_independent"] or 0, r["pf_useless_independent"] or 0
                if u + x == 0:
                    continue
                a = u / (u + x)
                if lo <= a < hi and r["independent_vs_noPF"]:
                    lv = [int(v) for v in str(r["epochs_at_level_0_1_2_3"]).split()]
                    sel.append((r["speedup_vs_independent"], 1 - lv[3] / sum(lv) if sum(lv) else 0, r["independent_vs_noPF"]))
            n.append(len(sel))
            sp.append(gm([s[0] for s in sel]) if sel else float("nan"))
            thr.append(st.mean(s[1] for s in sel) if sel else float("nan"))
            ben.append(gm([s[2] for s in sel]) if sel else float("nan"))
        keep = [i for i, k in enumerate(n) if k > 0]
        bl = [bins[i] for i in keep]
        n, sp, thr, ben = [n[i] for i in keep], [sp[i] for i in keep], [thr[i] for i in keep], [ben[i] for i in keep]
        x = range(len(bl))
        ax.bar([i - 0.2 for i in x], [100 * (v - 1) for v in sp], 0.4, color="#08519c", label="Core speedup under throttling (\\%)".replace("\\", ""))
        ax.bar([i + 0.2 for i in x], [100 * (v - 1) for v in ben], 0.4, color="#bdbdbd", label="Benefit of its own prefetcher (%)")
        ax2 = ax.twinx()
        ax2.plot(list(x), [100 * v for v in thr], "o-", color="#d94801", ms=3, label="Time throttled (%)")
        ax2.set_ylim(0, 100)
        ax2.spines["top"].set_visible(False)
        ax.axhline(0, color="black", lw=0.6)
        ax.set_xticks(list(x))
        ax.set_xticklabels(["%.1f-%.1f\n(n=%d)" % (lo, min(hi, 1), k) for (lo, hi), k in zip(bl, n)])
        ax.set_xlabel("Prefetch accuracy of the core (independent run)")
        ax.set_title(PNAME[pf])
        if pf == "ipcp":
            ax.set_ylabel("Change vs reference (%)")
        else:
            ax2.set_ylabel("Time throttled (%)")
        if pf == "ipcp":
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax2.get_legend_handles_labels()
    fig.legend(h1 + h2, l1 + l2, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.08))
    fig.tight_layout()
    fig.savefig(FIG / "root_cause_accuracy.pdf")
    plt.close(fig)
    print("wrote figures/root_cause_accuracy.pdf")


# ------------------------------------------------------------------ Final 4-core results
VARS = ("vpt_part", "psr", "v22", "v3", "v3w", "v32", "v33")


def matched(rows, s, p, need=("vpt_part", "psr", "v22", "v32", "v33")):
    by = collections.defaultdict(dict)
    for r in rows:
        if r["setup"] == s and r["prefetcher"] == p:
            by[r["mix"]][r["variant"]] = r
    return {m: d for m, d in by.items() if all(v in d for v in need)}


def stats(rs):
    return dict(nws=gm([r["NWS_vs_independent"] for r in rs]), hs=gm([r["HS_vs_independent"] for r in rs]),
                worst=min(r["NWS_vs_independent"] for r in rs), bad=sum(r["NWS_vs_independent"] < 0.98 for r in rs),
                cas_i=sum(r["casualties_independent"] for r in rs), cas_c=sum(r["casualties_coord"] for r in rs),
                wc_i=st.mean(r["independent_worst_vs_noPF"] for r in rs), wc_c=st.mean(r["coord_worst_vs_noPF"] for r in rs))


def final_table():
    rows = sheet(D / "final_4core.xlsx", "Per mix")
    t = ["\\begin{tabular}{llcccccc}", "\\toprule",
         "& Design & Throughput & HS & Worst mix & Mixes $<-2\\%$ & Casualties & Worst core \\\\", "\\midrule"]
    for s, sname in (("4core_lowbw", "Low bandwidth (2.4 GB/s/core)"), ("4core", "Ample bandwidth (9.6 GB/s/core)")):
        for p in ("ipcp", "pythia"):
            M = matched(rows, s, p)
            t.append("\\multicolumn{8}{l}{\\emph{%s, %s, %d mixes}} \\\\" % (sname, PNAME[p], len(M)))
            for v in VARS:
                rs = [d[v] for d in M.values() if v in d]
                if len(rs) < len(M):
                    continue
                x = stats(rs)
                t.append("& %s & %s & %s & %s & %d & %d$\\to$%d & %.2f$\\to$%.2f \\\\" % (
                    NAME[v], pct(x["nws"]), pct(x["hs"]), pct(x["worst"]), x["bad"], x["cas_i"], x["cas_c"], x["wc_i"], x["wc_c"]))
            t.append("\\midrule")
    t[-1] = "\\bottomrule"
    t.append("\\end{tabular}")
    write("final_4core.tex", "\n".join(t) + "\n")


def tradeoff_figure():
    rows = sheet(D / "final_4core.xlsx", "Per mix")
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.4))
    for ax, p in zip(axes, ("ipcp", "pythia")):
        M = matched(rows, "4core_lowbw", p)
        for v in VARS:
            rs = [d[v] for d in M.values() if v in d]
            if len(rs) < len(M):
                continue
            x = stats(rs)
            ax.scatter(100 * (x["nws"] - 1), x["bad"], s=40 if v == "v33" else 22, color=COL[v], zorder=3,
                       edgecolor="black" if v == "v33" else "none")
            ax.scatter([], [], s=22, color=COL[v], label=NAME[v].replace("\\textbf{", "").replace("}", ""))
        ax.axvline(0, color="black", lw=0.5)
        ax.set_xlabel("Throughput vs independent PF+UCP (%)")
        ax.set_ylabel("Mixes worse than $-2\\%$")
        ax.set_title("%s, 4-core, low bandwidth (%d mixes)" % (PNAME[p], len(M)))
    fig.tight_layout()
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper center", ncol=4, frameon=False, fontsize=6,
               bbox_to_anchor=(0.5, 1.12))
    fig.savefig(FIG / "tradeoff.pdf")
    plt.close(fig)
    print("wrote figures/tradeoff.pdf")


def permix_figure():
    rows = sheet(D / "final_4core.xlsx", "Per mix")
    fig, axes = plt.subplots(2, 1, figsize=(7.0, 3.6))
    for ax, p in zip(axes, ("ipcp", "pythia")):
        M = matched(rows, "4core_lowbw", p)
        mixes = sorted(M, key=lambda m: M[m]["v33"]["NWS_vs_independent"])
        w = 0.2
        for k, v in enumerate(("vpt_part", "psr", "v22", "v33")):
            ax.bar([i + (k - 1.5) * w for i in range(len(mixes))], [100 * (M[m][v]["NWS_vs_independent"] - 1) for m in mixes],
                   w, color=COL[v], label=NAME[v].replace("\\textbf{", "").replace("}", ""))
        ax.axhline(0, color="black", lw=0.5)
        ax.axhline(-2, color="#d94801", lw=0.5, ls="--")
        ax.set_xticks(range(len(mixes)))
        ax.set_xticklabels(["%d" % m for m in mixes])
        ax.set_ylabel("Throughput vs indep. (%)")
        ax.set_title("%s, 4-core, low bandwidth: per mix (sorted by TSC)" % PNAME[p])
        if p == "ipcp":
            ax.legend(frameon=False, ncol=4, loc="upper left")
    axes[-1].set_xlabel("Mix id")
    fig.tight_layout()
    fig.savefig(FIG / "permix_lowbw.pdf")
    plt.close(fig)
    print("wrote figures/permix_lowbw.pdf")


def byclass_figure():
    rows = sheet(D / "final_4core.xlsx", "By class")
    classes = ("HCHP", "HCLP", "LCHP", "LCLP")
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.2))
    for ax, p in zip(axes, ("ipcp", "pythia")):
        w = 0.2
        for k, v in enumerate(("vpt_part", "psr", "v22", "v33")):
            vals = []
            for c in classes:
                r = [x for x in rows if x["setup"] == "4core_lowbw" and x["prefetcher"] == p and x["variant"] == v and x["core_class"] == c]
                vals.append(100 * (r[0]["geomean_speedup_vs_independent"] - 1) if r else 0)
            ax.bar([i + (k - 1.5) * w for i in range(len(classes))], vals, w, color=COL[v],
                   label=NAME[v].replace("\\textbf{", "").replace("}", ""))
        ax.axhline(0, color="black", lw=0.5)
        ax.set_xticks(range(len(classes)))
        ax.set_xticklabels(classes)
        ax.set_title("%s, low bandwidth: per-core speedup by class" % PNAME[p])
        ax.set_ylabel("Core speedup vs indep. (%)")
        if p == "ipcp":
            ax.legend(frameon=False, fontsize=6)
    fig.tight_layout()
    fig.savefig(FIG / "byclass_lowbw.pdf")
    plt.close(fig)
    print("wrote figures/byclass_lowbw.pdf")


def sweep_table():
    """Sensitivity of TSC itself: accuracy-hold threshold (10 mixes) and own-core budget (22 mixes)."""
    sw = sheet(D / "final_4core.xlsx", "Sweep")
    t = ["\\begin{tabular}{lcrcc}", "\\toprule", "Parameter (default) & Value & Mixes & IPCP & Pythia \\\\", "\\midrule"]
    hold = [r for r in sw if str(r["parameter"]).startswith("HOLD") and r["setup"] == "4core_lowbw"]
    for val in sorted({r["value"] for r in hold}, key=float):
        g = {r["prefetcher"]: r for r in hold if r["value"] == val}
        t.append("Accuracy hold (0.15) & %s & %d & %+.1f\\%% & %+.1f\\%% \\\\" % (val, g["ipcp"]["mixes"],
                 g["ipcp"]["change_vs_reference_pct"], g["pythia"]["change_vs_reference_pct"]))
    rows = sheet(D / "final_4core.xlsx", "Per mix")
    ch = {}
    for p in ("ipcp", "pythia"):
        M = matched(rows, "4core_lowbw", p, need=("v33", "v33s3"))
        ch[p] = (len(M), 100 * (gm([M[m]["v33s3"]["NWS_vs_independent"] for m in M]) /
                                gm([M[m]["v33"]["NWS_vs_independent"] for m in M]) - 1))
    if ch["ipcp"][0]:
        t.append("Own-core budget (1\\%%) & 3\\%% & %d & %+.1f\\%% & %+.1f\\%% \\\\" % (ch["ipcp"][0], ch["ipcp"][1],
                 ch["pythia"][1]))
    t += ["\\bottomrule", "\\end{tabular}"]
    write("sweep.tex", "\n".join(t) + "\n")


def eightcore_table():
    rows = sheet(D / "v22_8core.xlsx", "Per mix")
    v33 = D / "v33_8core.xlsx"
    if v33.is_file():
        rows += [r for r in sheet(v33, "Per mix") if r["variant"] == "v33"]
    t = ["\\begin{tabular}{llcccccc}", "\\toprule",
         "& Design & Throughput & HS & Worst mix & Mixes $<-2\\%$ & Casualties & Worst core \\\\", "\\midrule"]
    for p in ("ipcp", "pythia"):
        M = matched(rows, "8core", p, need=("vpt_part", "v22"))
        n_all = len(M)
        n_v33 = sum(1 for d in M.values() if "v33" in d)
        if 0 < n_v33 < n_all:      # partial TSC run: compare every design on the mixes TSC has finished
            missing = sorted(m for m, d in M.items() if "v33" not in d)
            M = {m: d for m, d in M.items() if "v33" in d}
            t.append("\\multicolumn{8}{l}{\\emph{8-core, 2.4 GB/s/core, %s, %d of %d mixes (mix %s pending)}} \\\\"
                     % (PNAME[p], len(M), n_all, ", ".join("%02d" % m for m in missing)))
        else:
            t.append("\\multicolumn{8}{l}{\\emph{8-core, 2.4 GB/s/core, %s, %d mixes}} \\\\" % (PNAME[p], len(M)))
        for v in ("vpt_part", "psr", "v22", "v33"):
            rs = [d[v] for d in M.values() if v in d]
            if v == "v33" and len(rs) < len(M):
                t.append("& %s & \\multicolumn{6}{c}{\\emph{pending (%d of %d mixes done)}} \\\\" % (NAME[v], len(rs), len(M)))
                continue
            if len(rs) < len(M):
                continue
            x = stats(rs)
            t.append("& %s & %s & %s & %s & %d & %d$\\to$%d & %.2f$\\to$%.2f \\\\" % (
                NAME[v], pct(x["nws"]), pct(x["hs"]), pct(x["worst"]), x["bad"], x["cas_i"], x["cas_c"], x["wc_i"], x["wc_c"]))
        t.append("\\midrule")
    t[-1] = "\\bottomrule"
    t.append("\\end{tabular}")
    write("eightcore.tex", "\n".join(t) + "\n")


# ------------------------------------------------------------------ Headroom: uniform grid and per-core oracle
DEG = ("native", "d4", "d2", "d1", "off")
DEGL = {"native": "native", "d4": "cap 4", "d2": "cap 2", "d1": "cap 1", "off": "PF off"}
PART = ("ucp", "none", "eq")
PARTL = {"ucp": "UCP", "none": "no partition", "eq": "equal ways"}


def grid_data():
    """Per-core IPC of the uniform-knob grid (data/grid_4core.csv, extracted from the characterisation reports).
    Returns {pf: (mixes, {(part, deg): {(mix, core): ipc}})} over the mixes present in every configuration."""
    import csv
    G = collections.defaultdict(lambda: collections.defaultdict(dict))
    with open(D / "grid_4core.csv") as f:
        for r in csv.DictReader(f):
            G[r["prefetcher"]][(r["partition"], r["degree"])][(int(r["mix"]), int(r["core"]))] = float(r["ipc"])
    out = {}
    for pf, cfg in G.items():
        mixes = sorted({m for m, _ in cfg[("ucp", "native")]})
        mixes = [m for m in mixes if all((m, k) in d for d in cfg.values() for k in range(4))]
        out[pf] = (mixes, cfg)
    return out


def grid_stats():
    """NWS and worst core of every uniform configuration vs independent (UCP + native PF), and the best uniform
    configuration chosen per mix."""
    res = {}
    for pf, (mixes, cfg) in grid_data().items():
        ind = cfg[("ucp", "native")]
        per = {}
        for key, d in cfg.items():
            nws = [st.mean(d[(m, k)] / ind[(m, k)] for k in range(4)) for m in mixes]
            wc = [min(d[(m, k)] / ind[(m, k)] for k in range(4)) for m in mixes]
            per[key] = (gm(nws), st.mean(wc), sum(x >= 1.02 for x in nws), nws)
        best = [max(per[k][3][i] for k in per if k != ("ucp", "native")) for i in range(len(mixes))]
        res[pf] = (mixes, per, best)
    return res


def grid_figure():
    S = grid_stats()
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 1.9))
    for ax, pf in zip(axes, ("ipcp", "pythia")):
        mixes, per, _ = S[pf]
        M = [[100 * (per[(p, d)][0] - 1) for d in DEG] for p in PART]
        # vector rectangles (imshow renders with diagonal seams in some PDF viewers)
        im = ax.pcolormesh(M, cmap="RdBu", vmin=-12, vmax=12, edgecolors="white", linewidth=0.5,
                           antialiased=False)
        ax.set_xlim(0, len(DEG))
        ax.set_ylim(len(PART), 0)
        for i, p in enumerate(PART):
            for j, d in enumerate(DEG):
                v, n = M[i][j], per[(p, d)][2]
                txt = "indep." if (p, d) == ("ucp", "native") else "%s\n%d mix%s" % (pct(1 + v / 100).replace("\\%", "%"), n, "" if n == 1 else "es")
                ax.text(j + 0.5, i + 0.5, txt.replace("\\n", "\n"), ha="center", va="center", fontsize=6,
                        color="white" if abs(v) > 8 else "black")
        ax.set_xticks([j + 0.5 for j in range(len(DEG))], [DEGL[d] for d in DEG])
        ax.set_yticks([i + 0.5 for i in range(len(PART))], [PARTL[p] for p in PART] if pf == "ipcp" else [])
        ax.set_title("%s, %d mixes" % (PNAME[pf], len(mixes)))
        for s in ax.spines.values():
            s.set_visible(False)
    cb = fig.colorbar(im, ax=axes, fraction=0.025, pad=0.02)
    cb.set_label("throughput vs independent (%)")
    fig.savefig(FIG / "grid_headroom.pdf")
    plt.close(fig)
    print("wrote figures/grid_headroom.pdf")
    for pf in ("ipcp", "pythia"):
        mixes, per, best = S[pf]
        bk = max((k for k in per if k != ("ucp", "native")), key=lambda k: per[k][0])
        print("  grid %s: %d mixes, best single config %s %s; per-mix best %s, >=2%%: %d, >=3%%: %d, max %s; "
              "worst core D1/D2/D4 (UCP) %.3f/%.3f/%.3f" % (pf, len(mixes), bk, pct(per[bk][0]), pct(gm(best)),
              sum(x >= 1.02 for x in best), sum(x >= 1.03 for x in best), pct(max(best)),
              per[("ucp", "d1")][1], per[("ucp", "d2")][1], per[("ucp", "d4")][1]))


def knob_rows():
    rows = sheet(D / "oracle_4core.xlsx", "Knob headroom")
    k = {"pf": "prefetch only (UCP adaptive ways)", "part": "partition only (native prefetcher)",
         "both": "both knobs (coordination)"}
    out = collections.defaultdict(list)
    for r in rows:
        if r["prefetcher"] in ("ipcp", "pythia"):
            v = {n: (float(r[c]) if r[c] is not None else None) for n, c in k.items()}
            out[r["prefetcher"]].append((int(r["mix"]), v, r["winner"]))
    return out


def oracle_numbers(o4):
    """Numbers quoted in Section 3 for the 4-core oracle, as macros (tables/oracle_numbers.tex), so the text follows
    the data when data/oracle_4core.xlsx is replaced."""
    num = lambda x: ("%.1f" % (100 * (x - 1))).replace("-0.0", "0.0")
    m = {"OrcIpN": int(o4["ipcp"][1]), "OrcPyN": int(o4["pythia"][1]),
         "OrcIpMean": num(float(o4["ipcp"][2])), "OrcPyMean": num(float(o4["pythia"][2])),
         "OrcMax": num(max(float(o4["ipcp"][3]), float(o4["pythia"][3]))),
         "OrcIpThree": int(o4["ipcp"][4]), "OrcPyThree": int(o4["pythia"][4])}
    K = knob_rows()
    frag = {}
    for p, key in (("ipcp", "Ip"), ("pythia", "Py")):
        rs = K[p]
        m["Kn%sN" % key] = len(rs)
        m["Kn%sPf" % key] = num(gm([v["pf"] for _, v, _ in rs]))
        m["Kn%sPart" % key] = num(gm([v["part"] for _, v, _ in rs]))
        m["Kn%sBest" % key] = num(gm([max(x for x in v.values() if x is not None) for _, v, _ in rs]))
        m["Kn%sBoth" % key] = sum(1 for _, _, w in rs if w and w.startswith("both"))
        frag[p] = [mx for mx, v, _ in rs if v["both"] is not None and v["both"] < min(v["pf"], v["part"]) - 0.01]
    def mixes(p):
        f = frag[p]
        if not f:
            return ""
        return "%s mix%s %s" % (PNAME[p], "es" if len(f) > 1 else "",
                                ", ".join(map(str, f[:-1])) + (" and " if len(f) > 1 else "") + str(f[-1]))
    m["KnFragile"] = ", ".join(x for x in (mixes("ipcp"), mixes("pythia")) if x) or "none"
    (TAB / "oracle_numbers.tex").write_text("".join("\\newcommand{\\%s}{%s}\n" % (k, v) for k, v in m.items()))
    print("wrote tables/oracle_numbers.tex: %s" % m)


def oracle_runs():
    """Every run of the 4-core oracle study: data/oracle_runs.csv (copied from oracle_results/) when present,
    otherwise the 'All runs' tab of data/oracle_4core.xlsx."""
    import csv
    if (D / "oracle_runs.csv").exists():
        with open(D / "oracle_runs.csv") as f:
            rows = list(csv.DictReader(f))
    else:
        R = list(openpyxl.load_workbook(D / "oracle_4core.xlsx", read_only=True, data_only=True)["All runs"]
                 .iter_rows(values_only=True))
        rows = [dict(zip(R[0], r)) for r in R[1:] if r and r[0]]
    out = []
    for r in rows:
        try:
            r["NWS"], r["worst_core"], r["mix"] = float(r["NWS"]), float(r["worst_core"]), int(r["mix"])
        except (TypeError, ValueError, KeyError):
            continue
        r["role"] = r.get("role") or ""
        out.append(r)
    return out


REF = {"lru_pf": "PF, LRU", "ucp_nopf": "UCP, no PF", "lru_nopf": "LRU, no PF"}
REFOFF = {"lru_pf": (-2, -6, "right"), "ucp_nopf": (3, 1, "left"), "lru_nopf": (3, -5, "left")}


def scatter_panel(ax, search, final, ref, title):
    ax.scatter([r["worst_core"] for r in search], [r["NWS"] for r in search], s=4, color="#9aa5b1", lw=0,
               label="searched (short runs)", zorder=1)
    ax.scatter([r["worst_core"] for r in final], [r["NWS"] for r in final], s=14, color="#2b7bba", lw=0,
               label="finalists (validated)", zorder=3)
    for r in ref:
        ax.scatter([r["worst_core"]], [r["NWS"]], s=12, marker="s", color="#e09f2a", lw=0, zorder=2,
                   label="reference configurations")
        dx, dy, ha = REFOFF.get(r["config"], (2, 1, "left"))
        ax.annotate(REF.get(r["config"], r["config"]), (r["worst_core"], r["NWS"]), fontsize=4.5,
                    xytext=(dx, dy), textcoords="offset points", ha=ha)
    ax.axhline(1, color="#c0392b", lw=0.5, zorder=0)
    ax.axvline(1, color="#c0392b", lw=0.5, zorder=0)
    ax.scatter([1], [1], marker="*", s=40, color="#c0392b", zorder=4, label="independent PF+UCP")
    ax.set_title(title, fontsize=5.8, pad=2)
    ax.tick_params(labelsize=5, length=2, pad=1)


def oracle_scatter_figures():
    runs = oracle_runs()
    mixname = {}
    for r in runs:
        lg = str(r.get("log") or "")
        parts = [x for x in lg.split("/") if x.startswith("mix%02d_" % r["mix"])]
        if parts:
            mixname[r["mix"]] = parts[-1].replace(".txt", "")
    for pf in ("ipcp", "pythia"):
        rs = [r for r in runs if r["prefetcher"] == pf]
        mixes = sorted({r["mix"] for r in rs})
        nc = 5
        nr = math.ceil(len(mixes) / nc)
        fig, axes = plt.subplots(nr, nc, figsize=(7.0, 1.45 * nr))
        axes = list(axes.flat)
        for ax, m in zip(axes, mixes):
            mr = [r for r in rs if r["mix"] == m]
            search = [r for r in mr if r["phase"] == "search"]
            final = [r for r in mr if r["phase"] == "final" and r["role"].startswith("finalist")]
            ref = [r for r in mr if r["phase"] == "final" and r["config"] in REF]
            scatter_panel(ax, search, final, ref, re.sub(r"(?<=_)1x|(?<=_)(\d)x", r"\1", mixname.get(m, "mix%02d" % m)).replace("_", " "))
        for ax in axes[len(mixes):]:
            ax.axis("off")
        for ax in axes[::nc]:
            ax.set_ylabel("throughput", fontsize=6)
        for ax in axes[(nr - 1) * nc:]:
            ax.set_xlabel("worst-core speedup", fontsize=6)
        h, l = axes[0].get_legend_handles_labels()
        uniq = dict(zip(l, h))
        fig.legend(uniq.values(), uniq.keys(), loc="upper center", ncol=4, frameon=False, fontsize=6,
                   bbox_to_anchor=(0.5, 1.03))
        fig.tight_layout(h_pad=0.6, w_pad=0.4)
        fig.savefig(FIG / ("oracle_scatter_%s.pdf" % pf))
        plt.close(fig)
        nval = len({r["mix"] for r in rs if r["phase"] == "final" and r["role"].startswith("finalist")})
        print("wrote figures/oracle_scatter_%s.pdf (%d mixes, %d validated)" % (pf, len(mixes), nval))
    # 8-core
    R = list(openpyxl.load_workbook(D / "scaleup_8core.xlsx", read_only=True, data_only=True)["Oracle runs"]
             .iter_rows(values_only=True))
    r8 = [dict(zip(R[0], r)) for r in R[1:] if r and r[0] == "ipcp" and r[8] is not None]
    for r in r8:
        r["NWS"], r["worst_core"] = float(r["NWS"]), float(r["worst_core"])
    mixes = sorted({r["mix"] for r in r8})
    fig, axes = plt.subplots(2, 4, figsize=(7.0, 2.9))
    axes = list(axes.flat)
    for ax, m in zip(axes, mixes):
        mr = [r for r in r8 if r["mix"] == m]
        scatter_panel(ax, [r for r in mr if r["phase"] == "search"], [r for r in mr if r["phase"] == "validated"],
                      [], "mix%02d" % m)
    for ax in axes[::4]:
        ax.set_ylabel("throughput", fontsize=6)
    for ax in axes[4:]:
        ax.set_xlabel("worst-core speedup", fontsize=6)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper center", ncol=3, frameon=False, fontsize=6, bbox_to_anchor=(0.5, 1.04))
    fig.tight_layout(h_pad=0.6, w_pad=0.4)
    fig.savefig(FIG / "oracle_scatter_8core.pdf")
    plt.close(fig)
    print("wrote figures/oracle_scatter_8core.pdf")


def oracle_figure():
    K = knob_rows()
    fig, axes = plt.subplots(2, 1, figsize=(7.0, 3.0))
    cols = {"pf": "#2b7bba", "part": "#e09f2a", "both": "#2e8b57"}
    lab = {"pf": "prefetch knob only (UCP ways)", "part": "partition knob only (native prefetcher)",
           "both": "both knobs (coordination)"}
    for ax, pf in zip(axes, ("ipcp", "pythia")):
        rs = K[pf]
        for j, n in enumerate(("pf", "part", "both")):
            xs = [i + (j - 1) * 0.27 for i in range(len(rs))]
            ys = [100 * (v[n] - 1) if v[n] is not None else 0 for _, v, _ in rs]
            ax.bar(xs, ys, 0.27, color=cols[n], label=lab[n])
        ax.set_xticks(range(len(rs)), ["mix%02d" % m for m, _, _ in rs], fontsize=6)
        ax.axhline(0, color="k", lw=0.5)
        ax.set_ylabel("best throughput vs\nindependent (%)")
        ax.set_title("4-core, UCP + %s: where is the headroom? (search length)" % PNAME[pf], pad=3)
        print("  oracle4 %s: %d mixes; geomean pf-only %s, part-only %s, both %s, best %s; both wins %d" % (
            pf, len(rs), pct(gm([v["pf"] for _, v, _ in rs])), pct(gm([v["part"] for _, v, _ in rs])),
            pct(gm([v["both"] for _, v, _ in rs if v["both"] is not None])),
            pct(gm([max(x for x in v.values() if x is not None) for _, v, _ in rs])),
            sum(1 for _, _, w in rs if w and w.startswith("both"))))
    fig.tight_layout(h_pad=0.8)
    fig.legend(*axes[0].get_legend_handles_labels(), loc="upper center", ncol=3, frameon=False, fontsize=6.5,
               bbox_to_anchor=(0.5, 1.06))
    fig.savefig(FIG / "oracle_knobs.pdf")
    plt.close(fig)
    print("wrote figures/oracle_knobs.pdf")

# ------------------------------------------------------------------ 8-core per-mix and per-class figures
def rows8(tab):
    """8-core rows of FDT, FDT+PSR, FDT+Reclaim (v22_8core.xlsx) and TSC (v33_8core.xlsx, if present)."""
    rows = [r for r in sheet(D / "v22_8core.xlsx", tab) if r["setup"] == "8core"]
    if (D / "v33_8core.xlsx").is_file():
        rows += [r for r in sheet(D / "v33_8core.xlsx", tab) if r["setup"] == "8core" and r["variant"] == "v33"]
    return rows


def designs8(rows):
    return [v for v in ("vpt_part", "psr", "v22", "v33") if any(r["variant"] == v for r in rows)]


def permix_8core_figure():
    rows = rows8("Per mix")
    vs = designs8(rows)
    fig, axes = plt.subplots(2, 1, figsize=(7.0, 3.6))
    for ax, p in zip(axes, ("ipcp", "pythia")):
        M = matched(rows, "8core", p, need=tuple(vs))
        key = "v33" if "v33" in vs else vs[0]
        mixes = sorted(M, key=lambda m: M[m][key]["NWS_vs_independent"])
        w = 0.8 / len(vs)
        for k, v in enumerate(vs):
            ax.bar([i + (k - (len(vs) - 1) / 2) * w for i in range(len(mixes))],
                   [100 * (M[m][v]["NWS_vs_independent"] - 1) for m in mixes], w, color=COL[v],
                   label=NAME[v].replace("\\textbf{", "").replace("}", ""))
        ax.axhline(0, color="black", lw=0.5)
        ax.axhline(-2, color="#d94801", lw=0.5, ls="--")
        ax.set_xticks(range(len(mixes)))
        ax.set_xticklabels(["%d" % m for m in mixes])
        ax.set_ylabel("Throughput vs indep. (%)")
        ax.set_title("%s, 8-core, 2.4 GB/s/core: per mix (%d mixes, sorted by %s)" % (
            PNAME[p], len(mixes), "TSC" if key == "v33" else "FDT"))
        if p == "ipcp":
            ax.legend(frameon=False, ncol=len(vs), loc="upper left")
    axes[-1].set_xlabel("Mix id")
    fig.tight_layout()
    fig.savefig(FIG / "permix_8core.pdf")
    plt.close(fig)
    print("wrote figures/permix_8core.pdf (%s)" % ", ".join(vs))


def byclass_8core_figure():
    mixrows = rows8("Per mix")
    vs = designs8(mixrows)
    cores = rows8("Per core")
    classes = ("HCHP", "HCLP", "LCHP", "LCLP")
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.2))
    for ax, p in zip(axes, ("ipcp", "pythia")):
        mixes = set(matched(mixrows, "8core", p, need=tuple(vs)))     # same mixes for every design
        w = 0.8 / len(vs)
        for k, v in enumerate(vs):
            vals = []
            for c in classes:
                x = [r["speedup_vs_independent"] for r in cores if r["prefetcher"] == p and r["variant"] == v
                     and r["core_class"] == c and r["mix"] in mixes and r["speedup_vs_independent"]]
                vals.append(100 * (gm(x) - 1) if x else 0)
            ax.bar([i + (k - (len(vs) - 1) / 2) * w for i in range(len(classes))], vals, w, color=COL[v],
                   label=NAME[v].replace("\\textbf{", "").replace("}", ""))
        ax.axhline(0, color="black", lw=0.5)
        ax.set_xticks(range(len(classes)))
        ax.set_xticklabels(classes)
        ax.set_title("%s, 8-core, 2.4 GB/s/core: per-core speedup by class" % PNAME[p])
        ax.set_ylabel("Core speedup vs indep. (%)")
        if p == "ipcp":
            ax.legend(frameon=False, fontsize=6)
    fig.tight_layout()
    fig.savefig(FIG / "byclass_8core_designs.pdf")
    plt.close(fig)
    print("wrote figures/byclass_8core_designs.pdf (%s)" % ", ".join(vs))


# ------------------------------------------------------------------ Appendix tables: per-mix static oracle (4 and 8 cores)
def cfg_str(c):
    """'w1-1-1-13_d6-6-6-0' -> 'ways 1/1/1/13; c3 off'  ('wucp' = UCP's adaptive ways, degree 6 = native, 0 = off)."""
    if not c:
        return "--"
    w, d = c.split("_d") if "_d" in c else (c, "")
    ways = "UCP" if w == "wucp" else "ways " + w[1:].replace("-", "/")
    caps = [int(x) for x in d.split("-")] if d else []
    off = [i for i, x in enumerate(caps) if x != 6]
    if not off:
        deg = ""
    elif len(off) == len(caps) and len(set(caps)) == 1:
        deg = "all off" if caps[0] == 0 else "all cap %d" % caps[0]
    else:
        deg = ", ".join("c%d %s" % (i, "off" if caps[i] == 0 else "cap %d" % caps[i]) for i in off)
    return ways + ("; " + deg if deg else "")


def oracle_tables():
    R = list(openpyxl.load_workbook(D / "oracle_4core.xlsx", read_only=True, data_only=True)["Summary"]
             .iter_rows(values_only=True))
    hi = next(i for i, r in enumerate(R) if r and r[0] == "prefetcher" and "best config (throughput)" in r)
    fin = {(r[0], int(r[1])): r for r in R[hi + 1:] if r and r[0] in ("ipcp", "pythia") and isinstance(r[1], (int, float))}
    K = knob_rows()
    f3 = lambda x: "%.3f" % float(x) if x is not None else "--"
    t = ["\\begin{tabular}{llccclcclcc}", "\\toprule",
         " & & \\multicolumn{3}{c}{Search length: best of} & \\multicolumn{3}{c}{Best validated (throughput)} & "
         "\\multicolumn{3}{c}{Fairest validated, no throughput loss} \\\\",
         "\\cmidrule(lr){3-5}\\cmidrule(lr){6-8}\\cmidrule(lr){9-11}",
         "PF & Mix & PF knob & Partition & Both & Configuration & Thr. & Worst & Configuration & Thr. & Worst \\\\",
         "\\midrule"]
    for pf in ("ipcp", "pythia"):
        for m, v, _ in K[pf]:
            r = fin.get((pf, m))
            t.append("%s & %d & %s & %s & %s & %s & %s & %s & %s & %s & %s \\\\" % (
                PNAME[pf], m, f3(v["pf"]), f3(v["part"]), f3(v["both"]),
                cfg_str(r[3]) if r else "not validated", f3(r[4]) if r else "--", f3(r[5]) if r else "--",
                cfg_str(r[6]) if r else "--", f3(r[7]) if r else "--", f3(r[8]) if r else "--"))
        t.append("\\midrule")
    t[-1] = "\\bottomrule"
    t.append("\\end{tabular}")
    write("oracle_4core_mixes.tex", "\n".join(t) + "\n")

    R8 = list(openpyxl.load_workbook(D / "scaleup_8core.xlsx", read_only=True, data_only=True)["Oracle"]
              .iter_rows(values_only=True))
    rows = [r for r in R8 if r and r[0] == "ipcp" and isinstance(r[1], int) and isinstance(r[3], str)]
    summ = next(r for r in R8 if r and r[0] == "ipcp" and isinstance(r[1], int) and isinstance(r[2], (int, float)))
    R8r = list(openpyxl.load_workbook(D / "scaleup_8core.xlsx", read_only=True, data_only=True)["Oracle runs"]
               .iter_rows(values_only=True))
    h = R8r[0]
    runs = [dict(zip(h, r)) for r in R8r[1:] if r and r[0] == "ipcp"]
    n = collections.Counter(r["kind"] for r in runs if r["phase"] == "search")
    t = ["\\begin{tabular}{rlccclcc}", "\\toprule",
         " & \\multicolumn{4}{c}{Best validated (throughput)} & \\multicolumn{3}{c}{Fairest validated, no throughput loss} \\\\",
         "\\cmidrule(lr){2-5}\\cmidrule(lr){6-8}",
         "Mix & Configuration & Knob & Thr. & Worst & Configuration & Thr. & Worst \\\\", "\\midrule"]
    for r in rows:
        t.append("%d & %s & %s & %.3f & %.3f & %s & %.3f & %.3f \\\\" % (
            r[1], cfg_str(r[3]), r[4], float(r[5]), float(r[6]), cfg_str(r[8]), float(r[9]), float(r[10])))
    t += ["\\midrule",
          "\\multicolumn{8}{l}{Search length, mean of the best per mix: prefetch knob only %.3f, partition only %.3f, "
          "both knobs %.3f} \\\\" % (float(summ[6]), float(summ[7]), float(summ[8])),
          "\\multicolumn{8}{l}{Configurations evaluated in total over the %d mixes: prefetch only %d, partition only %d, "
          "both knobs %d} \\\\" % (len(rows), n.get("prefetch only", 0), n.get("partition only", 0), n.get("both knobs", 0)),
          "\\bottomrule", "\\end{tabular}"]
    write("oracle_8core_mixes.tex", "\n".join(t) + "\n")


if __name__ == "__main__":
    char_table()
    scaleup_table()
    oracle_table()
    root_cause_figure()
    final_table()
    tradeoff_figure()
    permix_figure()
    byclass_figure()
    sweep_table()
    eightcore_table()
    permix_8core_figure()
    byclass_8core_figure()
    grid_figure()
    oracle_figure()
    oracle_scatter_figures()
    oracle_tables()
