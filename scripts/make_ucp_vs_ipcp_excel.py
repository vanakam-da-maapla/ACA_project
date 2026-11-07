#!/usr/bin/env python3
"""
make_ucp_vs_ipcp_excel.py
==============================================================================
Converts the logs of the 4-core experiments written by run_4core_mixes_ipcp.py into ONE Excel workbook and compares
them, exactly like make_ucp_vs_pythia_excel.py did for UCP / UCP_PYTHIA / PYTHIA:

    UCP              UCP partitioning, no prefetcher               results_4core_mixes_ipcp/ucp                (baseline)
    UCP_IPCP_L2      UCP + IPCP at the L2C only                    results_4core_mixes_ipcp/ucp_ipcp_l2
    UCP_IPCP_L1_L2   UCP + IPCP at L1D and L2C                     results_4core_mixes_ipcp/ucp_ipcp_l1_l2
    IPCP_L2          IPCP at the L2C only, plain SRRIP/LRU LLC     results_4core_mixes_ipcp/ipcp_l2_srrip   (or ipcp_l2_lru)
    IPCP_L1_L2       IPCP at L1D and L2C, plain SRRIP/LRU LLC      results_4core_mixes_ipcp/ipcp_l1_l2_srrip (or ..._lru)
  (optional extras, used automatically when their folders have logs:  UCP_IPCP_L1, IPCP_L1 = IPCP at the L1D only)

Experiments without logs are skipped (UCP is required: it is the baseline).  Every metric is compared pairwise:
    X over UCP   for every other experiment X        (what the prefetcher / UCP+prefetcher gains over UCP alone)
    UCP_IPCP_L2 over IPCP_L2,  UCP_IPCP_L1_L2 over IPCP_L1_L2   (isolate what UCP partitioning adds on top of IPCP)

    python3 make_ucp_vs_ipcp_excel.py                                  # default folders under ./results_4core_mixes_ipcp
    python3 make_ucp_vs_ipcp_excel.py --results-root /path/results_4core_mixes_ipcp --out my.xlsx
    python3 make_ucp_vs_ipcp_excel.py --dir ucp=/other/ucp_logs        # use another folder for one experiment
    python3 make_ucp_vs_ipcp_excel.py --skip ipcp_l2 ucp_ipcp_l2       # leave experiments out
    python3 make_ucp_vs_ipcp_excel.py --alone-xlsx IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx

Workbook tabs (same as the UCP_PYTHIA workbook, with one more tab: IPCP Classes)
  Comparison        PLOT FIRST, TABLE BELOW, one block per metric: a bar plot with every mix (all experiments side by
                    side) and, directly under it, the per-mix table with the % improvement of each pair.  Metrics: total IPC,
                    weighted / relative / harmonic speedup, worst-case core, max slowdown, fairness, LLC / L2C misses, MPKI,
                    LLC hit rate, LLC miss latency, LLC OCCUPANCY (all cores together + stacked per-core plot per experiment
                    + one plot per core slot), prefetch accuracy and aggressiveness (L1D + L2C), DRAM reads, row-buffer hit rate.
  Summary           headline averages, then averages by mix type, by number of high-prefetch / high-cache cores and by trace class.
  Single-Core View  the single-core metrics: per trace (and per trace class) IPC alone vs in the mixes, normalised IPC, speed-up,
                    MPKI, hit rate, miss latency, LLC occupancy, prefetch accuracy / aggressiveness -- plots + tables.
  Group Plots       plots of the average improvement per mix type / number of high-prefetch / high-cache cores.
  Core Plots        PER CORE, PER MIX: choose a mix in the yellow cell -> one plot per metric with its 4 cores and one bar per
                    experiment (IPC, normalised IPC, slowdown, MPKI, misses, miss latency, hit rate, prefetch accuracy / PKI,
                    LLC occupancy, unused prefetches, UCP quota) + table with the % improvement of each pair.
  Core Plots (all)  the same plots for all cores of all mixes at once (wide plots: mix01 c0 ... mix50 c3).
  IPCP Classes      IPCP only: prefetches filled / useful / late and accuracy per IPCP class (GS, CS, CPLX, NL) at the L1D and the
                    L2C, summed over all mixes and cores, per experiment (+ plots).
  Mix Comparison    one row per mix, every compared quantity of all experiments (formulas).
  Core Comparison   one row per core of every mix (IPC speed-up, miss changes, prefetch accuracy, LLC occupancy).
  <EXPERIMENT>      every statistic of every core of every mix, taken from the logs (one tab per experiment)
  <EXPERIMENT>_mix  per-mix data of the logs: DRAM statistics, UCP final quotas, run time
  Alone IPC         single-core IPC per trace used for weighted speedup (editable)
  Notes             definitions, conventions and warnings

LLC occupancy and the Core_<n>_* statistics come from the statistics hook that run_4core_mixes_ipcp.py --build adds to the
simulator; logs of binaries built without it cannot be converted.

Colour code: blue = number parsed from a log, black = formula.
Only the Python standard library + openpyxl are needed.
==============================================================================
"""
import argparse
import csv
import re
import sys
from collections import OrderedDict
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import CharacterProperties, Paragraph, ParagraphProperties, RichTextProperties
from openpyxl.formatting.rule import CellIsRule
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as CL
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.workbook.properties import CalcProperties

NC = 4  # cores per mix
FONT = "Arial"
F_TITLE = PatternFill("solid", fgColor="1F4E78")
F_SUB = PatternFill("solid", fgColor="5B9BD5")
F_LIGHT = PatternFill("solid", fgColor="D9EAF7")
F_LIGHT2 = PatternFill("solid", fgColor="EEF5FB")
F_GREY = PatternFill("solid", fgColor="F2F2F2")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def fnt(bold=False, size=10, color="000000", italic=False):
    return Font(name=FONT, size=size, bold=bold, color=color, italic=italic)


def q(name):
    return "'%s'" % name


def num(s):
    return int(s) if re.fullmatch(r"-?\d+", s) else float(s)


# ==========================================================================
# 1) Parsing the simulator logs
# ==========================================================================
CORE_RE = re.compile(r"^Core_(\d+)_(\S+)\s+(\S+)\s*$", re.M)
CHAN_RE = re.compile(r"^Channel_(\d+)_(\S+)\s+(\d+)\s*$", re.M)
UCP_RE = re.compile(r"^\[UCP\] completed partitions=(\d+) last quotas:(.*)$", re.M)
FIN_RE = re.compile(r"^Finished CPU (\d+) .*?Simulation time: (\d+) hr (\d+) min (\d+) sec", re.M)
KNOB_RE = re.compile(r"^(warmup_instructions|simulation_instructions|num_cpus|llc_set|llc_way|dram_channels|"
                     r"champsim_seed)\s+(\d+)\s*$", re.M)
SCOOBY_KEYS = ["predict_called", "train_called", "pref_issue_scooby", "reward_correct_timely",
               "reward_correct_untimely", "reward_incorrect", "reward_none", "reward_out_of_bounds"]


class Log:
    pass


def parse_log(path):
    text = Path(path).read_text(errors="replace")
    lg = Log()
    lg.name, lg.path = Path(path).parent.name, str(path)
    lg.traces = [Path(m.group(1)).name.replace(".champsimtrace.xz", "")
                 for m in re.finditer(r"^CPU \d+ runs (\S+)", text, re.M)]
    lg.cfg = {m.group(1): int(m.group(2)) for m in KNOB_RE.finditer(text)}
    for k, rx in (("warmup_instructions", r"^Warmup Instructions:\s*(\d+)"), ("simulation_instructions", r"^Simulation Instructions:\s*(\d+)"),
                  ("num_cpus", r"^Number of CPUs:\s*(\d+)")):
        mm = re.search(rx, text, re.M)
        if mm:
            lg.cfg[k] = int(mm.group(1))
    lg.pf = sorted(set(re.findall(r"^adding L2C_PREFETCHER: (\S+)", text, re.M)))
    lg.cores = {}
    for m in CORE_RE.finditer(text):               # first occurrence wins (= ROI statistics)
        lg.cores.setdefault(int(m.group(1)), OrderedDict()).setdefault(m.group(2), num(m.group(3)))
    ncores = lg.cfg.get("num_cpus", NC)
    lg.complete = ("ChampSim completed all CPUs" in text
                   and all("IPC" in lg.cores.get(c, {}) for c in range(ncores)))
    u = UCP_RE.search(text)
    lg.ucp = None
    if u:
        lg.ucp = {"partitions": int(u.group(1)),
                  "quotas": {int(a): int(b) for a, b in re.findall(r"CPU(\d+)=(\d+)", u.group(2))}}
    lg.chan = {}
    for m in CHAN_RE.finditer(text):
        lg.chan.setdefault(int(m.group(1)), OrderedDict())[m.group(2)] = int(m.group(3))
    lg.dram = {}
    for key in ("avg_congested_cycle", "DRAM_bw_pochs"):
        m = re.search(r"^%s\s+(\d+)\s*$" % key, text, re.M)
        lg.dram[key] = int(m.group(1)) if m else None
    lg.levels = {int(a): int(b) for a, b in re.findall(r"^DRAM_bw_level_(\d+)\s+(\d+)\s*$", text, re.M)}
    secs = [int(h) * 3600 + int(mi) * 60 + int(s) for _, h, mi, s in FIN_RE.findall(text)]
    lg.sim_min = round(max(secs) / 60, 1) if secs else None
    # Pythia (Scooby) per-core counters: one block per core, in core order
    lg.scooby = {}
    pos = [m.start() for m in re.finditer(r"^scooby_predict_called\s", text, re.M)]
    for i, p in enumerate(pos):
        block = text[p: pos[i + 1] if i + 1 < len(pos) else len(text)]
        d = {}
        for k in SCOOBY_KEYS:
            m = re.search(r"^scooby_%s\s+(\d+)\s*$" % k, block, re.M)
            d[k] = int(m.group(1)) if m else None
        lg.scooby[i] = d
    return lg


def load_experiment(folder):
    logs, bad = OrderedDict(), []
    for f in sorted(Path(folder).glob("mix*/mix*.txt")):
        if f.name != f.parent.name + ".txt":      # skip FAILED/TIMEOUT variants
            continue
        lg = parse_log(f)
        (logs.__setitem__(lg.name, lg) if lg.complete else bad.append(lg.name))
    return logs, bad


def load_meta(folders, logs_a):
    """mix name -> dict(id, type, n_hc, n_hp, classes[4]); from mixes.csv, else derived from the names."""
    for d in folders:
        f = Path(d) / "mixes.csv"
        if f.is_file():
            meta = {}
            for r in csv.DictReader(open(f)):
                meta[r["mix_name"]] = dict(
                    id=int(r["mix_id"]), type=r["type"], n_hc=int(r["n_high_cache"]), n_hp=int(r["n_high_prefetch"]),
                    classes=[r["core%d_class" % c] for c in range(NC)])
            return meta
    meta = {}
    for n in logs_a:
        m = re.match(r"mix(\d+)", n)
        meta[n] = dict(id=int(m.group(1)) if m else 0, type="", n_hc=None, n_hp=None, classes=[""] * NC)
    return meta


def load_alone(xlsx, way, deg):
    """single-core IPC per trace from the characterization workbook ('Raw Data' tab)."""
    wb = load_workbook(xlsx, data_only=True, read_only=True)
    out = {}
    if "Raw Data" in wb.sheetnames:
        rows = wb["Raw Data"].iter_rows(values_only=True)
        hdr = next(rows)
        ix = {h: i for i, h in enumerate(hdr) if h}
        for r in rows:
            if r and r[ix["Trace"]] and r[ix["Way"]] == way and r[ix["Degree"]] == deg:
                out[r[ix["Trace"]]] = float(r[ix["IPC"]])
    elif "IPC Surface" in wb.sheetnames:
        rows = wb["IPC Surface"].iter_rows(values_only=True)
        hdr = next(rows)
        col = hdr.index("W%d_D%d" % (way, deg))
        for r in rows:
            if r and r[0] and isinstance(r[col], (int, float)):
                out[r[0]] = float(r[col])
    return out


# ==========================================================================
# 2) Sheet writers
# ==========================================================================
def hdr_cell(ws, r, c, text, fill=F_TITLE, width=None, wrap=True):
    cell = ws.cell(r, c, text)
    cell.font, cell.fill, cell.border = fnt(True, 9, "FFFFFF"), fill, BOX
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=wrap)
    if width:
        ws.column_dimensions[CL(c)].width = width
    return cell


def title_block(ws, title, subtitle, ncols):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    ws["A1"] = title
    ws["A1"].font, ws["A1"].fill = fnt(True, 15, "FFFFFF"), F_TITLE
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 26
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    ws["A2"] = subtitle
    ws["A2"].font, ws["A2"].fill = fnt(size=9), F_LIGHT
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[2].height = 40


def cf_posneg(ws, rng):
    """green = positive (improvement), red = negative (degradation)"""
    ws.conditional_formatting.add(rng, CellIsRule(operator="greaterThan", formula=["0"], font=Font(name=FONT, color="007A33")))
    ws.conditional_formatting.add(rng, CellIsRule(operator="lessThan", formula=["0"], font=Font(name=FONT, color="C00000")))


HDR_ROW, FIRST = 5, 6


def write_exp_sheet(wb, name, logs, meta, alone_col, alone_sheet, raw_keys, has_scooby, has_ucp=True, occ_cfg=None):
    """One row per (mix, core) with every statistic of the log.  Returns column map + row map."""
    ws = wb.create_sheet(name)
    mixes = sorted(logs, key=lambda n: meta[n]["id"])
    ident = ["Mix ID", "Mix", "Type", "Core", "Trace", "Class"]
    derived = [("ipc", "IPC = instructions / cycles", "0.0000"),
               ("l1d_load_mpki", "L1D load MPKI", "0.00"), ("l2c_load_mpki", "L2C load MPKI", "0.00"),
               ("llc_load_mpki", "LLC load MPKI", "0.00"), ("llc_total_mpki", "LLC total MPKI", "0.00"),
               ("llc_load_missrate", "LLC load miss rate", "0.0%"), ("llc_hitrate", "LLC hit rate (all accesses)", "0.0%"),
               ("l2c_pf_acc", "Prefetch accuracy (L1D+L2C) = useful / (useful + useless)", "0.0%"),
               ("l2c_pf_pki", "L1D+L2C prefetches issued per 1000 instructions (PKI)", "0.00")]
    if occ_cfg:                                    # (LLC blocks, LLC sets): only if the logs carry LLC-occupancy statistics
        derived += [("llc_occ_share", "LLC occupancy share = avg blocks owned by the core / LLC blocks", "0.0%"),
                    ("llc_occ_ways", "LLC occupancy in ways = avg blocks owned / LLC sets", "0.00"),
                    ("llc_occ_pf", "Unused-prefetch blocks held by the core / LLC blocks", "0.0%")]
    if alone_col:
        derived += [("alone", "IPC alone (from 'Alone IPC' tab)", "0.0000"),
                    ("norm", "Normalised IPC = IPC / IPC alone", "0.0000"),
                    ("slow", "Slowdown = IPC alone / IPC", "0.0000")]
    extra = [("ucp_quota", "UCP final quota (ways)")] if has_ucp else []   # no UCP columns for non-UCP experiments
    if has_scooby:
        extra += [("sc_" + k, "Pythia: " + k) for k in SCOOBY_KEYS]
    ncol_front = len(ident) + len(derived) + len(extra)
    col = {}
    c = len(ident) + 1
    for k, _, _ in derived:
        col[k] = CL(c)
        c += 1
    for k, _ in extra:
        col[k] = CL(c)
        c += 1
    raw_col = {}
    for k in raw_keys:
        raw_col[k] = CL(c)
        c += 1
    last_col = c - 1

    title_block(ws, "%s - statistics of every core of every mix (from the simulator logs)" % name,
                "One row per mix and core. Blue = value parsed from the log, black = formula. "
                "Left: key derived metrics. Right (after the 'all logged statistics' marker): every Core_<n>_<stat> value "
                "printed by the simulator, unchanged.", min(last_col, 20))
    ws.cell(4, len(ident) + 1, "Derived metrics (formulas)").font = fnt(True, 10, "1F4E78")
    ws.cell(4, len(ident) + len(derived) + 1, "From the log").font = fnt(True, 10, "1F4E78")
    ws.cell(4, ncol_front + 1, "ALL LOGGED PER-CORE STATISTICS (raw)").font = fnt(True, 10, "1F4E78")
    for i, h in enumerate(ident, 1):
        hdr_cell(ws, HDR_ROW, i, h, width=[8, 34, 18, 6, 22, 8][i - 1])
    for k, h, _ in derived:
        hdr_cell(ws, HDR_ROW, ws[col[k] + "1"].column, h, F_SUB, 15)
    for k, h in extra:
        hdr_cell(ws, HDR_ROW, ws[col[k] + "1"].column, h, F_SUB, 14)
    for k in raw_keys:
        hdr_cell(ws, HDR_ROW, ws[raw_col[k] + "1"].column, k, F_TITLE, 14)
    ws.row_dimensions[HDR_ROW].height = 52

    rowmap, first_row = {}, {}
    r = FIRST
    for mn in mixes:
        lg, mt = logs[mn], meta[mn]
        first_row[mn] = r
        for core in range(NC):
            rowmap[(mn, core)] = r
            vals = [mt["id"], mn, mt["type"], core, lg.traces[core] if core < len(lg.traces) else "", mt["classes"][core]]
            for i, v in enumerate(vals, 1):
                ws.cell(r, i, v).font = fnt()
            d = lg.cores[core]
            R = lambda key: "%s%d" % (raw_col[key], r)
            F = {
                "ipc": "=IFERROR(%s/%s,\"\")" % (R("instructions"), R("cycles")),
                "l1d_load_mpki": "=IFERROR(%s/%s*1000,\"\")" % (R("L1D_load_miss"), R("instructions")),
                "l2c_load_mpki": "=IFERROR(%s/%s*1000,\"\")" % (R("L2C_load_miss"), R("instructions")),
                "llc_load_mpki": "=IFERROR(%s/%s*1000,\"\")" % (R("LLC_load_miss"), R("instructions")),
                "llc_total_mpki": "=IFERROR(%s/%s*1000,\"\")" % (R("LLC_total_miss"), R("instructions")),
                "llc_load_missrate": "=IFERROR(%s/%s,\"\")" % (R("LLC_load_miss"), R("LLC_loads")),
                "llc_hitrate": "=IFERROR(%s/%s,\"\")" % (R("LLC_total_hit"), R("LLC_total_access")),
                "l2c_pf_acc": "=IFERROR((%s+%s)/(%s+%s+%s+%s),\"\")" % (
                    R("L1D_prefetch_useful"), R("L2C_prefetch_useful"), R("L1D_prefetch_useful"), R("L1D_prefetch_useless"),
                    R("L2C_prefetch_useful"), R("L2C_prefetch_useless")),
                "l2c_pf_pki": "=IFERROR((%s+%s)/%s*1000,\"\")" % (R("L1D_prefetch_issued"), R("L2C_prefetch_issued"), R("instructions")),
            }
            if alone_col:
                F["alone"] = "=IFERROR(INDEX(%s!$%s:$%s,MATCH($E%d,%s!$A:$A,0)),\"\")" % (
                    q(alone_sheet), alone_col, alone_col, r, q(alone_sheet))
                F["norm"] = "=IFERROR(%s%d/%s%d,\"\")" % (col["ipc"], r, col["alone"], r)
                F["slow"] = "=IFERROR(%s%d/%s%d,\"\")" % (col["alone"], r, col["ipc"], r)
            if occ_cfg:
                F["llc_occ_share"] = "=IFERROR(%s/%d,\"\")" % (R("LLC_occupancy_avg_blocks"), occ_cfg[0])
                F["llc_occ_ways"] = "=IFERROR(%s/%d,\"\")" % (R("LLC_occupancy_avg_blocks"), occ_cfg[1])
                F["llc_occ_pf"] = "=IFERROR(%s/%d,\"\")" % (R("LLC_occupancy_avg_prefetch_blocks"), occ_cfg[0])
            for k, _, fmt in derived:
                cell = ws[col[k] + str(r)]
                cell.value, cell.number_format, cell.font = F[k], fmt, fnt()
            if has_ucp and lg.ucp and core in lg.ucp["quotas"]:
                ws[col["ucp_quota"] + str(r)].value = lg.ucp["quotas"][core]
                ws[col["ucp_quota"] + str(r)].font = fnt(color="0000FF")
            if has_scooby and core in lg.scooby:
                for k in SCOOBY_KEYS:
                    cell = ws[col["sc_" + k] + str(r)]
                    cell.value, cell.font, cell.number_format = lg.scooby[core][k], fnt(color="0000FF"), "#,##0"
            for k in raw_keys:
                if k in d:
                    cell = ws[raw_col[k] + str(r)]
                    cell.value, cell.font = d[k], fnt(color="0000FF")
                    cell.number_format = "#,##0" if isinstance(d[k], int) else "0.00000"
            r += 1
    last_row = r - 1
    ws.freeze_panes = ws.cell(FIRST, 7)
    ws.auto_filter.ref = "A%d:%s%d" % (HDR_ROW, CL(last_col), last_row)
    allcol = dict(col)
    allcol.update({"raw:" + k: v for k, v in raw_col.items()})
    return ws, allcol, rowmap, first_row, last_row


def write_mix_sheet(wb, name, logs, meta, channels, levels, has_ucp=True):
    """One row per mix: DRAM statistics, UCP final quotas, run time."""
    ws = wb.create_sheet(name)
    mixes = sorted(logs, key=lambda n: meta[n]["id"])
    cols = ["Mix ID", "Mix", "Type", "Sim time (min)", "Prefetchers (L1D / L2C)", "LLC sets", "LLC ways", "Warm-up instr/core",
            "Sim instr/core", "champsim_seed"]
    if has_ucp:                                    # UCP columns only exist for UCP experiments
        cols += ["UCP partitions"] + ["UCP quota core%d" % c for c in range(NC)]
    chkeys = ["RQ_row_buffer_hit", "RQ_row_buffer_miss", "WQ_row_buffer_hit", "WQ_row_buffer_miss", "WQ_full", "dbus_congested"]
    for k in channels:
        cols += ["ch%d_%s" % (k, s) for s in chkeys]
    cols += ["avg_congested_cycle", "DRAM_bw_epochs"] + ["DRAM_bw_level_%d" % i for i in levels]
    nraw = len(cols)
    cols += ["DRAM read requests (RQ hit+miss)", "DRAM write requests (WQ hit+miss)", "DRAM RQ row-buffer hit rate",
             "% of bandwidth epochs in the top-2 levels"]
    title_block(ws, "%s - per-mix data of the logs (DRAM, UCP partitioning, run time)" % name,
                "One row per mix. Blue = parsed from the log, black = formula.", min(len(cols), 14))
    for i, h in enumerate(cols, 1):
        hdr_cell(ws, HDR_ROW, i, h, F_TITLE if i <= nraw else F_SUB, 11 if i > 3 else [8, 34, 18][i - 1])
    ws.row_dimensions[HDR_ROW].height = 52
    pos = {h: CL(i) for i, h in enumerate(cols, 1)}
    rowmap = {}
    for i, mn in enumerate(mixes):
        r = FIRST + i
        rowmap[mn] = r
        lg = logs[mn]
        vals = [meta[mn]["id"], mn, meta[mn]["type"], lg.sim_min, "/".join(lg.pf), lg.cfg.get("llc_set"), lg.cfg.get("llc_way"),
                lg.cfg.get("warmup_instructions"), lg.cfg.get("simulation_instructions"), lg.cfg.get("champsim_seed")]
        if has_ucp:
            vals += [lg.ucp["partitions"] if lg.ucp else None]
            vals += [lg.ucp["quotas"].get(c) if lg.ucp else None for c in range(NC)]
        for k in channels:
            vals += [lg.chan.get(k, {}).get(s) for s in chkeys]
        vals += [lg.dram.get("avg_congested_cycle"), lg.dram.get("DRAM_bw_pochs")] + [lg.levels.get(i) for i in levels]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r, c, v)
            cell.font = fnt(color="0000FF" if c > 3 else "000000")
            if isinstance(v, int) and c > 7:
                cell.number_format = "#,##0"
        rq_hit = "+".join("%s%d" % (pos["ch%d_RQ_row_buffer_hit" % k], r) for k in channels)
        rq_miss = "+".join("%s%d" % (pos["ch%d_RQ_row_buffer_miss" % k], r) for k in channels)
        wq = "+".join("%s%d+%s%d" % (pos["ch%d_WQ_row_buffer_hit" % k], r, pos["ch%d_WQ_row_buffer_miss" % k], r) for k in channels)
        top2 = "+".join("%s%d" % (pos["DRAM_bw_level_%d" % i], r) for i in levels[-2:]) if levels else None
        F = ["=%s+%s" % (rq_hit, rq_miss), "=%s" % wq, "=IFERROR((%s)/(%s+%s),\"\")" % (rq_hit, rq_hit, rq_miss),
             ("=IFERROR((%s)/%s%d,\"\")" % (top2, pos["DRAM_bw_epochs"], r)) if top2 else "n/a"]
        for j, f in enumerate(F):
            cell = ws.cell(r, nraw + 1 + j, f)
            cell.font = fnt()
            cell.number_format = "#,##0" if j < 2 else "0.0%"
    ws.freeze_panes = ws.cell(FIRST, 4)
    return ws, pos, rowmap


def write_alone(wb, names, traces, alone, way, deg):
    """One 'IPC alone' column per experiment.  Column B (first experiment) holds the characterization values,
    the other columns start as formulas '=B<row>' and can be overwritten with experiment-specific values."""
    ws = wb.create_sheet("Alone IPC")
    n = len(names)
    others = ", ".join(names[1:])
    title_block(ws, "Single-core IPC per trace (denominator of the weighted speedup)",
                "Source: characterization sweep, 1 core, %d LLC ways, prefetch degree D%d (D0 = no prefetcher), 2048 LLC sets. "
                "The columns for %s start equal to column B; overwrite a column with the IPC of each trace running alone with "
                "that experiment's configuration if you have it, and every weighted-speedup number of that experiment updates."
                % (way, deg, others), n + 2)
    for i, h in enumerate(["Trace"] + ["IPC alone - %s baseline" % nm for nm in names] + ["Found"], 1):
        hdr_cell(ws, HDR_ROW, i, h, width=26 if i == 1 else 22 if i <= n + 1 else 10)
    for i, t in enumerate(sorted(traces)):
        r = FIRST + i
        ws.cell(r, 1, t).font = fnt()
        if t in alone:
            ws.cell(r, 2, alone[t]).font = fnt(color="0000FF")
            for j in range(1, n):
                ws.cell(r, 2 + j, "=B%d" % r).font = fnt()
            ws.cell(r, n + 2, "yes").font = fnt()
        else:
            ws.cell(r, n + 2, "MISSING").font = fnt(color="C00000")
        for j in range(n):
            ws.cell(r, 2 + j).number_format = "0.000000"
    ws.freeze_panes = "B6"
    return ws


def manifest_info(folder):
    """(binary name, LLC replacement policy, L1D prefetcher, L2C prefetcher) of the last run recorded in <folder>/run_manifest.txt."""
    f = Path(folder) / "run_manifest.txt"
    if not f.is_file():
        return None, None, None, None
    text = f.read_text(errors="replace")
    last = lambda key: (re.findall(r"^%s\s*:\s*(\S+)" % key, text, re.M) or [None])[-1]
    b = last("binary")
    return (Path(b).name if b else None), last("repl"), last("l1d_pref"), last("l2c_pref")


class Exp:
    """One experiment (= one results folder) taking part in the comparison."""

    def __init__(self, tag, name, folder, logs, bad):
        self.tag, self.name, self.dir, self.logs, self.bad = tag, name, Path(folder), logs, bad
        self.has_ucp = any(lg.ucp for lg in logs.values())
        self.has_scooby = any(lg.scooby for lg in logs.values())
        occ = ["LLC_occupancy_avg_blocks" in lg.cores.get(0, {}) for lg in logs.values()]
        self.has_occ, self.occ_partial = any(occ), any(occ) and not all(occ)
        self.has_pf = any((lg.cores[c].get("L1D_prefetch_issued") or 0) + (lg.cores[c].get("L2C_prefetch_issued") or 0) > 0
                          for lg in logs.values() for c in lg.cores)
        self.has_bw = any(lg.levels for lg in logs.values())
        self.binary, self.repl, self.l1d, self.l2c = manifest_info(folder)
        for lg in logs.values():
            lg.pf = ["L1D=%s" % (self.l1d or "?"), "L2C=%s" % (self.l2c or "?")]

    def col(self, key):                           # column letter on this experiment's per-core tab
        return self.cols[key] if key in self.cols else self.cols["raw:" + key]

    def rng(self, key, mn):                       # the 4 core cells of one mix, as a formula range
        c = self.col(key)
        return "%s!%s%d:%s%d" % (q(self.name), c, self.first[mn], c, self.first[mn] + NC - 1)


# ==========================================================================
# (tag, label, results sub-folders tried in order, prefetchers at L1D / L2C is read from run_manifest.txt)
REGISTRY = [("a", "UCP", ("ucp",)),
            ("b", "UCP_IPCP_L2", ("ucp_ipcp_l2",)),
            ("c", "UCP_IPCP_L1_L2", ("ucp_ipcp_l1_l2",)),
            ("d", "IPCP_L2", ("ipcp_l2_srrip", "ipcp_l2_lru")),
            ("e", "IPCP_L1_L2", ("ipcp_l1_l2_srrip", "ipcp_l1_l2_lru")),
            ("f", "UCP_IPCP_L1", ("ucp_ipcp_l1",)),
            ("g", "IPCP_L1", ("ipcp_l1_srrip", "ipcp_l1_lru"))]
KEY_OF = {"ucp": "a", "ucp_ipcp_l2": "b", "ucp_ipcp_l1_l2": "c", "ipcp_l2": "d", "ipcp_l1_l2": "e", "ucp_ipcp_l1": "f", "ipcp_l1": "g"}
# (X, Y): "X compared with Y" (improvement positive when X is better); only pairs whose two experiments exist are used
PAIR_SPEC = [("b", "a"), ("c", "a"), ("d", "a"), ("e", "a"), ("f", "a"), ("g", "a"),
             ("b", "d"), ("c", "e"), ("f", "g")]
UCP_EFFECT = {("b", "d"), ("c", "e"), ("f", "g")}        # same prefetchers, UCP vs plain LLC
COLOR_NAMES = ["grey", "orange", "green", "blue", "purple", "yellow", "red"]
EXP_COL = {"a": "8497B0", "b": "ED7D31", "c": "70AD47", "d": "5B9BD5", "e": "7030A0", "f": "FFC000", "g": "C00000"}
PAIR_COLS = ["5B9BD5", "FFC000", "7030A0", "ED7D31", "70AD47", "C00000", "8497B0", "00B0A0", "A5A5A5"]
ALONE_COL = "404040"                                         # chart colour of the single-core "alone" series


def emit_block(ws, r0, title, lead_headers, n_rows, lead_cell, series, imps, unit, chart_fmt,
               avg_label=None, rotate=False, band_cols=14, ch_rows=20, stacked=False, extra=None, max_w=60):
    """One 'plot above, table below' block starting at row r0 (the standard layout of the Comparison tabs).
       lead_headers : headers of the leading label columns; the first one is the chart's x axis
       lead_cell    : f(row, i) -> list of values / formulas of the leading columns of data row i
       series       : [dict(name, fmt, color, cell=f(row, i))]  one chart series (= one table column) each
       imps         : [dict(name, x, y, hib)]  '% improvement' columns computed from series x and y
                      (x/y - 1 when higher is better, 1 - x/y when lower is better)
       avg_label    : adds 'Average / # improved / # degraded' rows under the table when given
       stacked      : draw the series as one stacked bar per row (shares that add up to a total)
       extra        : [dict(name, fmt, cell=f(row, i))] table-only columns after the improvement columns (not plotted)
       Returns dict(row, hr, t0, t1, vcol=[letters of the series], icol=[letters of the improvement columns], next)."""
    nl, ns = len(lead_headers), len(series)
    extra = extra or []
    nx = len(extra)
    ws.merge_cells(start_row=r0, start_column=1, end_row=r0, end_column=max(band_cols, nl + ns + len(imps) + nx))
    h = ws.cell(r0, 1, title)
    h.font, h.fill = fnt(True, 12, "FFFFFF"), F_SUB
    h.alignment = Alignment(vertical="center")
    ws.row_dimensions[r0].height = 22
    hr = r0 + 1 + ch_rows                      # table header row
    t0, t1 = hr + 1, hr + n_rows
    for i, t in enumerate(lead_headers, 1):
        hdr_cell(ws, hr, i, t, F_TITLE)
    for j, s in enumerate(series):
        hdr_cell(ws, hr, nl + 1 + j, s["name"], F_SUB)
    for j, im in enumerate(imps):
        hdr_cell(ws, hr, nl + ns + 1 + j, im["name"], F_SUB)
    for j, ex in enumerate(extra):
        hdr_cell(ws, hr, nl + ns + len(imps) + 1 + j, ex["name"], F_SUB)
    ws.row_dimensions[hr].height = 44
    for i in range(n_rows):
        r = t0 + i
        for c, v in enumerate(lead_cell(r, i), 1):
            ws.cell(r, c, v)
        for j, s in enumerate(series):
            ws.cell(r, nl + 1 + j, s["cell"](r, i)).number_format = s["fmt"]
        for j, im in enumerate(imps):
            fx, fy = "%s%d" % (CL(nl + 1 + im["x"]), r), "%s%d" % (CL(nl + 1 + im["y"]), r)
            ws.cell(r, nl + ns + 1 + j, ("=IFERROR(%s/%s-1,\"\")" if im["hib"] else "=IFERROR(1-%s/%s,\"\")") % (fx, fy)).number_format = "0.00%"
        for j, ex in enumerate(extra):
            ws.cell(r, nl + ns + len(imps) + 1 + j, ex["cell"](r, i)).number_format = ex["fmt"]
        for c in range(1, nl + ns + len(imps) + nx + 1):
            ws.cell(r, c).font = fnt(color="000000")
            ws.cell(r, c).border = BOX
    nxt = t1 + 5
    if avg_label:
        lab_col = max(1, nl - 1)
        labels = [(avg_label, True)] + ([("Improved (#)", False), ("Degraded (#)", False)] if imps else [])
        for j, (lab, is_avg) in enumerate(labels):
            r = t1 + 1 + j
            ws.cell(r, lab_col, lab).font = fnt(True)
            if is_avg:
                for c in range(nl + 1, nl + ns + len(imps) + nx + 1):
                    cell = ws.cell(r, c, "=IFERROR(AVERAGE(%s%d:%s%d),\"\")" % (CL(c), t0, CL(c), t1))
                    cell.font, cell.fill = fnt(True), F_GREY
                    cell.number_format = (series[c - nl - 1]["fmt"] if c <= nl + ns else
                                          "0.00%" if c <= nl + ns + len(imps) else extra[c - nl - ns - len(imps) - 1]["fmt"])
            else:
                for k in range(len(imps)):
                    c = nl + ns + 1 + k
                    cell = ws.cell(r, c, "=COUNTIF(%s%d:%s%d,\"%s0\")" % (CL(c), t0, CL(c), t1, ">" if j == 1 else "<"))
                    cell.font, cell.fill = fnt(True), F_GREY
        nxt = t1 + 1 + len(labels) + 3
    for k in range(len(imps)):
        c = nl + ns + 1 + k
        cf_posneg(ws, "%s%d:%s%d" % (CL(c), t0, CL(c), t1 + (1 if avg_label else 0)))
    # --- the plot (above the table): one bar series per table column
    ch = BarChart()
    ch.type, ch.grouping = "col", "clustered"
    ch.title = None
    ch.add_data(Reference(ws, min_col=nl + 1, max_col=nl + ns, min_row=hr, max_row=t1), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=t0, max_row=t1))
    for so, s in zip(ch.series, series):
        so.graphicalProperties.solidFill = s["color"]
        so.graphicalProperties.line.solidFill = s["color"]
    ch.gapWidth, ch.overlap = 60, 0
    if stacked:
        ch.grouping, ch.overlap, ch.gapWidth = "stacked", 100, 40
        ch.y_axis.scaling.min, ch.y_axis.scaling.max = 0, 1
    ch.y_axis.title = unit
    ch.x_axis.title = lead_headers[0]
    ch.y_axis.number_format = chart_fmt
    ch.x_axis.delete = ch.y_axis.delete = False
    ch.x_axis.tickLblSkip = 1
    ch.x_axis.tickLblPos = "low"               # labels stay at the bottom even when bars go negative
    if rotate:                                 # long labels (trace names): rotate 45 degrees
        ch.x_axis.txPr = RichText(bodyPr=RichTextProperties(rot=-2700000, vert="horz"),
                                  p=[Paragraph(pPr=ParagraphProperties(defRPr=CharacterProperties(sz=800)),
                                               endParaRPr=CharacterProperties())])
    ch.legend.position = "t"
    ch.width = max(30 if n_rows >= 10 else 18, min(max_w, (0.22 + 0.25 * ns) * n_rows + 3))
    ch.height = 11 if rotate else 9.5
    ws.add_chart(ch, "A%d" % (r0 + 1))
    return dict(row=r0, hr=hr, t0=t0, t1=t1, vcol=[CL(nl + 1 + j) for j in range(ns)],
                icol=[CL(nl + ns + 1 + k) for k in range(len(imps))], next=nxt)


CORE_COLS = ["4472C4", "ED7D31", "A5A5A5", "FFC000"]         # chart colours of core 0..3 in the stacked occupancy plots


def write_index(ws, row0, links, per_row=8, start_col=2):
    """'Jump to' hyperlinks (label, target row) laid out in rows row0.. of the tab; returns the first free row."""
    ws.cell(row0, 1, "Jump to:").font = fnt(True, 10, "1F4E78")
    for i, (label, rr) in enumerate(links):
        c = ws.cell(row0 + i // per_row, start_col + i % per_row, "%d. %s" % (i + 1, label))
        c.hyperlink = "#'%s'!A%d" % (ws.title, rr)
        c.font = Font(name=FONT, size=9, color="0563C1", underline="single")
    return row0 + (len(links) + per_row - 1) // per_row


# ==========================================================================
def find_dir(res_root, names):
    """First results folder (of the candidate names) that has complete-looking logs."""
    for n in names:
        d = res_root / n
        if d.is_dir() and any(d.glob("mix*/mix*.txt")):
            return d
    return None


IPCP_CLASSES = [("GS", "GS (global stream)"), ("CS", "CS (constant stride)"), ("CPLX", "CPLX (complex stride)"),
                ("NL", "NL (next line)"), ("none", "unclassified (class 0)")]


def write_ipcp_sheet(ws, exps, common):
    """IPCP only: prefetches filled / useful / late per IPCP class, summed over all cores of the compared mixes."""
    title_block(ws, "IPCP classes - prefetch fills, useful and late prefetches per IPCP class (GS, CS, CPLX, NL)",
                "Sum over the 4 cores of the %d compared mixes, from the counters pref_filled / pref_useful / pref_late that "
                "IPCP keeps per class (taken from the logs, blue). 'Useful / filled' is the accuracy IPCP itself uses to throttle "
                "a class; it can exceed 100%% because a prefetched block that is hit twice (or was filled before the "
                "measurement started) counts as useful again. Experiments without IPCP at that cache level are not listed. "
                "Class 0 = prefetch fills whose metadata carried no IPCP class; it is listed so that the class rows add up "
                "to all fills." % len(common), 12)
    ws.column_dimensions["A"].width = 30
    for i in range(2, 9):
        ws.column_dimensions[CL(i)].width = 17
    r = 4
    shown = False
    for lvl in ("L1D", "L2C"):
        users = [e for e in exps if "ipcp" in ((e.l1d if lvl == "L1D" else e.l2c) or "").lower()]
        if not users:
            continue
        shown = True
        ws.cell(r, 1, "IPCP at the %s" % lvl).font = fnt(True, 12, "1F4E78")
        lvl_r0 = r
        r += 1
        tabs = {}
        for metric, label, fmt in (("filled", "Prefetch fills", "#,##0"), ("useful", "Useful prefetches", "#,##0"),
                                   ("late", "Late prefetches", "#,##0")):
            hdr_cell(ws, r, 1, "%s (%s)" % (label, lvl), width=30)
            for j, e in enumerate(users):
                hdr_cell(ws, r, 2 + j, e.name, F_SUB, 17)
            tabs[metric] = r
            for k, (ck, cl) in enumerate(IPCP_CLASSES):
                ws.cell(r + 1 + k, 1, cl).font = fnt()
                for j, e in enumerate(users):
                    tot = 0
                    for mn in common:
                        for c in range(NC):
                            tot += e.logs[mn].cores[c].get("%s_IPCP_%s_%s" % (lvl, ck, metric), 0) or 0
                    cell = ws.cell(r + 1 + k, 2 + j, tot)
                    cell.font, cell.number_format = fnt(color="0000FF"), fmt
            r += len(IPCP_CLASSES) + 2
        for metric, label, num_m, den_m in (("acc", "Useful / filled (accuracy)", "useful", "filled"),
                                            ("lat", "Late / useful", "late", "useful"),
                                            ("share", "Share of all fills of the experiment", "filled", None)):
            hdr_cell(ws, r, 1, "%s (%s)" % (label, lvl), width=30)
            for j, e in enumerate(users):
                hdr_cell(ws, r, 2 + j, e.name, F_SUB, 17)
            tabs[metric] = r
            for k, (ck, cl) in enumerate(IPCP_CLASSES):
                ws.cell(r + 1 + k, 1, cl).font = fnt()
                for j, e in enumerate(users):
                    col = CL(2 + j)
                    num_c = "%s%d" % (col, tabs[num_m] + 1 + k)
                    if den_m:
                        f = '=IFERROR(%s/%s%d,"")' % (num_c, col, tabs[den_m] + 1 + k)
                    else:
                        f = '=IFERROR(%s/SUM(%s%d:%s%d),"")' % (num_c, col, tabs["filled"] + 1, col, tabs["filled"] + len(IPCP_CLASSES))
                    cell = ws.cell(r + 1 + k, 2 + j, f)
                    cell.font, cell.number_format = fnt(), "0.0%"
            r += len(IPCP_CLASSES) + 2
        # plots: fills per class and accuracy per class, one bar per experiment
        for n_, (metric, title) in enumerate((("filled", "%s: prefetch fills per IPCP class" % lvl),
                                              ("acc", "%s: useful / filled per IPCP class" % lvl))):
            ch = BarChart()
            ch.type, ch.grouping = "col", "clustered"
            ch.title, ch.y_axis.title, ch.x_axis.title = title, "prefetches" if metric == "filled" else "useful / filled", "IPCP class"
            hr = tabs[metric]
            ch.add_data(Reference(ws, min_col=2, max_col=1 + len(users), min_row=hr, max_row=hr + len(IPCP_CLASSES)), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=hr + 1, max_row=hr + len(IPCP_CLASSES)))
            for s, e in zip(ch.series, users):
                s.graphicalProperties.solidFill = EXP_COL[e.tag]
            ch.y_axis.number_format = "#,##0" if metric == "filled" else "0%"
            ch.x_axis.delete, ch.y_axis.delete = False, False
            ch.height, ch.width = 8.5, 17
            ws.add_chart(ch, "I%d" % (lvl_r0 + 1 + n_ * 19))
        r += 2
    if not shown:
        ws.cell(4, 1, "No experiment with IPCP found.").font = fnt()
    ws.freeze_panes = "B4"


def main():
    here = Path(__file__).resolve().parent
    root = here.parent if here.name == "scripts" else here
    ap = argparse.ArgumentParser(
        description="Excel workbook comparing the 4-core experiments UCP, UCP_IPCP_L2, UCP_IPCP_L1_L2, IPCP_L2 and IPCP_L1_L2.")
    ap.add_argument("--results-root", type=Path, default=None,
                    help="folder with one sub-folder per experiment (default: ./results_4core_mixes_ipcp if it exists, else "
                         "<folder of this script>/results_4core_mixes_ipcp)")
    ap.add_argument("--dir", action="append", default=[], metavar="NAME=DIR",
                    help="use DIR as the results folder of experiment NAME (%s); repeatable" % ", ".join(sorted(KEY_OF)))
    ap.add_argument("--skip", nargs="+", default=[], metavar="NAME", help="leave these experiments out of the workbook")
    ap.add_argument("--alone-xlsx", type=Path, default=None,
                    help="characterization workbook with single-core IPCs (tab 'Raw Data'); default: search for "
                         "IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx in the current folder / script folder")
    ap.add_argument("--alone-way", type=int, default=16, help="LLC ways of the single-core baseline IPC")
    ap.add_argument("--alone-degree", type=int, default=0, help="prefetch degree of the single-core baseline IPC (0 = none)")
    ap.add_argument("--out", type=Path, default=None, help="output workbook (default: UCP_vs_IPCP_4core.xlsx)")
    args = ap.parse_args()

    res_root = args.results_root or (Path.cwd() / "results_4core_mixes_ipcp" if (Path.cwd() / "results_4core_mixes_ipcp").is_dir()
                                     else root / "results_4core_mixes_ipcp")
    overrides = {}
    for item in args.dir:
        k, _, d = item.partition("=")
        if k not in KEY_OF or not d:
            ap.error("--dir expects NAME=DIR with NAME one of: %s" % ", ".join(sorted(KEY_OF)))
        overrides[k] = Path(d)
    for k in args.skip:
        if k not in KEY_OF:
            ap.error("--skip: unknown experiment '%s' (one of: %s)" % (k, ", ".join(sorted(KEY_OF))))
    if "ucp" in args.skip:
        ap.error("the UCP experiment is the baseline and cannot be skipped")

    # ---------------- which experiments take part
    spec = []
    for tag, label, names in REGISTRY:
        key = next(k for k, t in KEY_OF.items() if t == tag)
        if key in args.skip:
            continue
        folder = overrides.get(key) or find_dir(res_root, names)
        if folder is None or not any(Path(folder).glob("mix*/mix*.txt")):
            if tag == "a":
                sys.exit("[ERROR] no UCP logs found (looked for %s/ucp/mixNN_*/mixNN_*.txt). Run:  python3 run_4core_mixes_ipcp.py "
                         "--experiment all   (or point to them with --dir ucp=DIR)" % res_root)
            if tag in "bcde":
                print("[INFO] no logs for %s (looked in %s/%s) -> left out" % (label, res_root, " or ".join(names)))
            continue
        spec.append((tag, label, Path(folder)))
    exps = []
    for tag, name, folder in spec:
        logs, bad = load_experiment(folder)
        if not logs:
            if tag == "a":
                sys.exit("[ERROR] no complete logs found for %s in %s (expected <dir>/mixNN_*/mixNN_*.txt)" % (name, folder))
            print("[INFO] %s: no complete logs in %s -> left out" % (name, folder))
            continue
        exps.append(Exp(tag, name, folder, logs, bad))
    if len(exps) < 2:
        sys.exit("[ERROR] need the UCP baseline and at least one more experiment with complete logs")
    E = {e.tag: e for e in exps}
    base = exps[0]
    A = base.name
    nm = {e.tag: e.name for e in exps}
    # pairs (x, y): "x compared with y"; improvement is positive when x is better than y
    pairs = [p for p in PAIR_SPEC if p[0] in E and p[1] in E]
    pname = lambda p: "%s over %s" % (nm[p[0]], nm[p[1]])
    out = args.out or Path("UCP_vs_IPCP_4core.xlsx")

    meta = load_meta([e.dir for e in exps], base.logs)
    for e in exps:
        for n in e.logs:
            meta.setdefault(n, dict(id=int(re.match(r"mix(\d+)", n).group(1)), type="", n_hc=None, n_hp=None, classes=[""] * NC))
    common_set = set.intersection(*[set(e.logs) for e in exps])
    common = sorted(common_set, key=lambda n: meta[n]["id"])
    if not common:
        sys.exit("[ERROR] no mix has a complete log in all experiments (%s)" % ", ".join(e.name for e in exps))

    warnings = []
    for e in exps:
        if e.bad:
            warnings.append("%s: %d mix log(s) incomplete and skipped: %s" % (e.name, len(e.bad), ", ".join(e.bad[:8])))
    left_out = sorted(set.union(*[set(e.logs) for e in exps]) - common_set, key=lambda n: meta[n]["id"])
    if left_out:
        warnings.append("%d mix(es) are complete in only some of the experiments and are left out of the comparison: %s%s"
                        % (len(left_out), ", ".join(left_out[:8]), " ..." if len(left_out) > 8 else ""))
    for n in common:
        for e in exps[1:]:
            if base.logs[n].traces != e.logs[n].traces:
                warnings.append("%s: %s and %s ran DIFFERENT traces!" % (n, A, e.name))
    sig = lambda lg: tuple(lg.cfg.get(k) for k in ("num_cpus", "llc_set", "llc_way", "warmup_instructions",
                                                    "simulation_instructions", "dram_channels"))
    sigs = {sig(lg) for e in exps for lg in e.logs.values()}
    if len(sigs) > 1:
        warnings.append("the logs do NOT share one setup (num_cpus, llc_set, llc_way, warmup, sim, dram_channels): %s" % sorted(sigs, key=str))
    for x, y in pairs:
        if (x, y) in UCP_EFFECT and (E[x].l1d, E[x].l2c) != (E[y].l1d, E[y].l2c):
            warnings.append("%s and %s were run with different prefetchers (L1D %s/%s, L2C %s/%s): %s is then not a clean UCP effect"
                            % (nm[x], nm[y], E[x].l1d, E[y].l1d, E[x].l2c, E[y].l2c, pname((x, y))))
    for e in exps:
        if e.tag in "bcf" and not e.has_ucp:
            warnings.append("%s logs contain no UCP partitioning output: it is meant to run UCP" % e.name)
        if e.tag in "deg" and e.has_ucp:
            warnings.append("%s logs contain UCP partitioning output: it is meant to be IPCP alone on a plain LLC" % e.name)
        if e.tag == "a" and e.has_pf:
            warnings.append("%s (the baseline) issued prefetches: it is meant to run without a prefetcher" % e.name)
    if len({(e.repl if e.tag in "deg" else None) for e in exps if e.tag in "deg"}) > 1:
        warnings.append("the IPCP-alone experiments use different LLC replacement policies: %s"
                        % ", ".join("%s=%s" % (e.name, e.repl) for e in exps if e.tag in "deg"))
    for e in exps:
        if e.occ_partial:
            warnings.append("%s: only some mixes have LLC-occupancy statistics (re-run the others with the rebuilt binary)" % e.name)
    if any(e.has_occ for e in exps) and not all(e.has_occ for e in exps):
        warnings.append("LLC occupancy is only available for %s (the other experiments were run with a binary built without "
                        "occupancy statistics): occupancy plots cover those experiments only"
                        % ", ".join(e.name for e in exps if e.has_occ))
    if not any(e.has_occ for e in exps):
        print("[INFO] the logs contain no LLC-occupancy statistics -> no occupancy plots. Rebuild with "
              "'python3 run_4core_mixes_ipcp.py --experiment all --build --build-only' and re-run to get them.")
    s0 = next(iter(sigs))
    print("Setup in the logs: cores=%s LLC=%s sets x %s ways, warm-up=%s, sim=%s, dram channels=%s" % s0)

    # ---------------- single-core IPC for weighted speedup
    alone, alone_note = {}, ""
    xl = args.alone_xlsx
    if xl is None:
        for cand in (Path.cwd() / "IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx", root / "IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx"):
            if cand.is_file():
                xl = cand
                break
    if xl and Path(xl).is_file():
        alone = load_alone(xl, args.alone_way, args.alone_degree)
        alone_note = "%s (way %d, degree D%d)" % (Path(xl).name, args.alone_way, args.alone_degree)
    else:
        warnings.append("no characterization workbook found (--alone-xlsx): weighted/harmonic speedup against single-core IPC "
                        "are omitted; the relative weighted speedup (%s = 1.0) is still computed" % A)
    all_traces = sorted({t for n in common for t in base.logs[n].traces})
    miss_alone = [t for t in all_traces if alone and t not in alone]
    if miss_alone:
        warnings.append("no single-core IPC for %d trace(s): %s" % (len(miss_alone), ", ".join(miss_alone[:6])))
    have_alone = bool(alone)

    raw_keys = list(OrderedDict.fromkeys(k for e in exps for lg in e.logs.values() for k in next(iter(lg.cores.values()))))
    channels = sorted({k for e in exps for lg in e.logs.values() for k in lg.chan})
    levels = sorted({k for e in exps for lg in e.logs.values() for k in lg.levels})

    # ---------------- workbook skeleton and per-experiment tabs
    wb = Workbook()
    wb.calculation = CalcProperties(fullCalcOnLoad=True)
    wsC = wb.active
    wsC.title = "Comparison"
    wsS = wb.create_sheet("Summary")
    wsV = wb.create_sheet("Single-Core View")
    wsG = wb.create_sheet("Group Plots")
    wsP = wb.create_sheet("Core Plots")
    wsA = wb.create_sheet("Core Plots (all)")
    wsI = wb.create_sheet("IPCP Classes")
    wsM = wb.create_sheet("Mix Comparison")
    wsK = wb.create_sheet("Core Comparison")
    alone_cols = [CL(2 + i) for i in range(len(exps))]
    for i, e in enumerate(exps):
        e.alone_col = alone_cols[i] if have_alone else None
        _, e.cols, e.rows, e.first, _ = write_exp_sheet(wb, e.name, e.logs, meta, e.alone_col, "Alone IPC", raw_keys,
                                                        e.has_scooby, e.has_ucp,
                                                        (s0[1] * s0[2], s0[1]) if e.has_occ else None)
    for e in exps:
        _, e.mpos, e.mrow = write_mix_sheet(wb, e.name + "_mix", e.logs, meta, channels, levels, e.has_ucp)
    if have_alone:
        write_alone(wb, [e.name for e in exps], all_traces, alone, args.alone_way, args.alone_degree)
    wsN = wb.create_sheet("Notes")
    write_ipcp_sheet(wsI, exps, common)

    # ---------------- Core Comparison (per core): one column registry, letters assigned before the formulas are written
    cc = OrderedDict()   # key -> (header, width, fmt)
    for k, h, w, f in (("id", "Mix ID", 8, "0"), ("mix", "Mix", 34, "@"), ("type", "Type", 18, "@"),
                       ("core", "Core", 6, "0"), ("trace", "Trace", 22, "@"), ("cls", "Class", 8, "@")):
        cc[k] = (h, w, f)
    CC_GROUPS = [   # (metric on the per-core tab, label, width, fmt, [(derived key, header, kind, fmt)])
        ("ipc", "IPC", 11, "0.0000", [("sp", "Speed-up %s/%s", "ratio", "0.0000"), ("chg", "IPC change %% %s vs %s", "chg", "0.00%")]),
        ("LLC_load_miss", "LLC load misses", 14, "#,##0", [("llcl_red", "LLC load miss reduction %% %s vs %s", "red", "0.00%")]),
        ("L2C_load_miss", "L2C load misses", 14, "#,##0", [("l2cl_red", "L2C load miss reduction %% %s vs %s", "red", "0.00%")]),
        ("LLC_total_miss", "LLC total misses", 14, "#,##0", [("llct_red", "LLC total miss reduction %% %s vs %s", "red", "0.00%")]),
        ("LLC_average_miss_latency", "LLC avg miss latency", 12, "0.0", [("lat_red", "LLC latency reduction %% %s vs %s", "red", "0.00%")]),
        ("llc_load_mpki", "LLC load MPKI", 11, "0.00", []),
        ("l2c_load_mpki", "L2C load MPKI", 11, "0.00", []),
        ("llc_hitrate", "LLC hit rate (all accesses)", 11, "0.0%", []),
    ]
    for m, label, w, f, derived in CC_GROUPS:
        for e in exps:
            cc["%s_%s" % (m, e.tag)] = ("%s %s" % (label, e.name), w, f)
        for dk, dh, kind, df in derived:
            for x, y in pairs:
                cc["%s_%s_%s" % (dk, x, y)] = (dh % (nm[x], nm[y]), 12, df)
    for e in exps:
        if e.has_pf:
            cc["pfacc_" + e.tag] = ("Prefetch accuracy (L1D+L2C) " + e.name, 12, "0.00%")
            cc["pfpki_" + e.tag] = ("L1D+L2C prefetches issued PKI " + e.name, 12, "0.00")
    for e in exps:
        if e.has_occ:
            cc["occ_share_" + e.tag] = ("LLC occupancy share of the core " + e.name, 12, "0.0%")
            cc["occ_ways_" + e.tag] = ("LLC occupancy of the core in ways " + e.name, 12, "0.00")
            cc["occpf_" + e.tag] = ("Unused-prefetch share of the LLC (core) " + e.name, 12, "0.0%")
    for e in exps:
        if e.has_ucp:
            cc["quota_" + e.tag] = ("UCP quota " + e.name, 9, "0")
    if have_alone:
        for e in exps:
            cc["alone_" + e.tag] = ("IPC alone (single-core) " + e.name, 11, "0.0000")
            cc["norm_" + e.tag] = ("Norm. IPC " + e.name, 11, "0.0000")
            cc["slow_" + e.tag] = ("Slowdown (alone/shared) " + e.name, 11, "0.0000")
    ccol = {k: CL(i) for i, k in enumerate(cc, 1)}
    title_block(wsK, "Core Comparison - %s" % " vs ".join(e.name for e in exps) + ", every core of every mix",
                "Speed-up X/Y = IPC(X) / IPC(Y). Reduction %% = 1 - X / Y (positive = fewer misses / lower latency with X). "
                "Compared pairs (X over Y): %s." % "; ".join(pname(p) for p in pairs), 12)
    for i, (k, (h, w, f)) in enumerate(cc.items(), 1):
        hdr_cell(wsK, HDR_ROW, i, h, F_TITLE if i <= 6 else F_SUB, w)
    wsK.row_dimensions[HDR_ROW].height = 64
    cc_row, r = {}, FIRST
    for mn in common:
        for core in range(NC):
            cc_row[(mn, core)] = r
            V = {"id": meta[mn]["id"], "mix": mn, "type": meta[mn]["type"], "core": core,
                 "trace": base.logs[mn].traces[core], "cls": meta[mn]["classes"][core]}
            for m, label, w, f, derived in CC_GROUPS:
                for e in exps:
                    V["%s_%s" % (m, e.tag)] = "=%s!%s%d" % (q(e.name), e.col(m), e.rows[(mn, core)])
                for dk, dh, kind, df in derived:
                    for x, y in pairs:
                        cx, cy = ccol["%s_%s" % (m, x)], ccol["%s_%s" % (m, y)]
                        if kind == "ratio":
                            V["%s_%s_%s" % (dk, x, y)] = "=IFERROR(%s%d/%s%d,\"\")" % (cx, r, cy, r)
                        elif kind == "chg":
                            V["%s_%s_%s" % (dk, x, y)] = "=IFERROR(%s%d-1,\"\")" % (ccol["sp_%s_%s" % (x, y)], r)
                        else:
                            V["%s_%s_%s" % (dk, x, y)] = "=IFERROR(1-%s%d/%s%d,\"\")" % (cx, r, cy, r)
            for e in exps:
                if e.has_occ:
                    for ck, sk in (("occ_share_", "llc_occ_share"), ("occ_ways_", "llc_occ_ways"), ("occpf_", "llc_occ_pf")):
                        V[ck + e.tag] = "=%s!%s%d" % (q(e.name), e.col(sk), e.rows[(mn, core)])
                if e.has_pf:
                    V["pfacc_" + e.tag] = "=%s!%s%d" % (q(e.name), e.col("l2c_pf_acc"), e.rows[(mn, core)])
                    V["pfpki_" + e.tag] = "=%s!%s%d" % (q(e.name), e.col("l2c_pf_pki"), e.rows[(mn, core)])
                if e.has_ucp:
                    V["quota_" + e.tag] = "=%s!%s%d" % (q(e.name), e.col("ucp_quota"), e.rows[(mn, core)])
                if have_alone:
                    V["alone_" + e.tag] = "=%s!%s%d" % (q(e.name), e.col("alone"), e.rows[(mn, core)])
                    V["norm_" + e.tag] = "=%s!%s%d" % (q(e.name), e.col("norm"), e.rows[(mn, core)])
                    V["slow_" + e.tag] = "=%s!%s%d" % (q(e.name), e.col("slow"), e.rows[(mn, core)])
            for k, (h, w, f) in cc.items():
                cell = wsK[ccol[k] + str(r)]
                cell.value, cell.font = V[k], fnt()
                if k not in ("id", "mix", "type", "core", "trace", "cls"):
                    cell.number_format = f
            r += 1
    kc_last = r - 1
    wsK.freeze_panes = "G6"
    wsK.auto_filter.ref = "A%d:%s%d" % (HDR_ROW, CL(len(cc)), kc_last)
    for k in cc:
        if re.match(r"(chg|llcl_red|l2cl_red|llct_red|lat_red)_", k):
            cf_posneg(wsK, "%s%d:%s%d" % (ccol[k], FIRST, ccol[k], kc_last))

    # ---------------- Mix Comparison (per mix)
    mc = OrderedDict()   # key -> (header, width, fmt)
    def add(key, header, width=12, fmt="0.000"):
        mc[key] = (header, width, fmt)
    def add_each(key, header, width, fmt, only=None):
        for e in exps:
            if only is None or only(e):
                add("%s_%s" % (key, e.tag), "%s %s" % (header, e.name), width, fmt)
    add("id", "Mix ID", 8, "0"); add("name", "Mix", 34, "@"); add("type", "Type", 18, "@")
    add("n_hc", "# High-cache cores", 9, "0"); add("n_hp", "# High-prefetch cores", 9, "0")
    add("bench", "Benchmarks (core0 | core1 | core2 | core3)", 52, "@")
    add_each("thr", "Total IPC", 11, "0.0000")
    add_each("ws", "Weighted speedup", 12, "0.0000")
    for x, y in pairs:
        sfx = "%s_%s" % (x, y)
        add("wsrel_" + sfx, "Relative weighted speedup (%s = 1) %s" % (nm[y], pname((x, y))), 13, "0.0000")
        add("hsrel_" + sfx, "Relative harmonic speedup (%s = 1) %s" % (nm[y], pname((x, y))), 13, "0.0000")
        add("minsp_" + sfx, "Lowest per-core speed-up %s" % pname((x, y)), 11, "0.0000")
        add("maxsp_" + sfx, "Highest per-core speed-up %s" % pname((x, y)), 11, "0.0000")
    add_each("hs", "Harmonic speedup", 12, "0.0000")
    add_each("msd", "Max slowdown", 11, "0.0000")
    add_each("llcl", "LLC load misses", 14, "#,##0")
    add_each("llct", "LLC total misses", 14, "#,##0")
    add_each("l2cl", "L2C load misses", 14, "#,##0")
    add_each("mpki", "LLC load MPKI", 11, "0.00")
    add_each("lat", "LLC avg miss latency", 12, "0.0")
    add_each("l2cmpki", "L2C load MPKI", 11, "0.00")
    add_each("llchit", "LLC hit rate (all accesses)", 11, "0.0%")
    add_each("pfacc", "Prefetch accuracy (L1D+L2C)", 12, "0.0%", only=lambda e: e.has_pf)
    add_each("pfpki", "L1D+L2C prefetches issued PKI", 12, "0.00", only=lambda e: e.has_pf)
    add_each("fair", "Fairness (min/max normalised IPC)", 12, "0.0000")
    has_occ_ = lambda e: e.has_occ
    add_each("occ", "LLC occupancy of all cores (% of LLC)", 12, "0.0%", only=has_occ_)
    add_each("occpf", "Unused-prefetch blocks (% of LLC)", 12, "0.0%", only=has_occ_)
    add_each("occmax", "Largest core share of the LLC", 12, "0.0%", only=has_occ_)
    add_each("occmin", "Smallest core share of the LLC", 12, "0.0%", only=has_occ_)
    add_each("dram", "DRAM reads", 14, "#,##0")
    add_each("rbh", "DRAM RQ row-buffer hit rate", 12, "0.0%")
    add_each("bw", "% bandwidth epochs in top-2 levels", 13, "0.0%")
    add_each("q", "UCP final quotas", 12, "@", only=lambda e: e.has_ucp)
    add_each("t", "Sim time (min)", 10, "0.0")
    mcol = {k: CL(i) for i, k in enumerate(mc, 1)}
    title_block(wsM, "Mix Comparison - %s, one row per mix" % " vs ".join(e.name for e in exps),
                "All values are formulas on the per-core tabs (%s), the per-mix tabs and the Core Comparison tab. "
                "Relative speedups use per-core IPC(X)/IPC(Y) for the pairs %s; weighted/harmonic speedup use the single-core IPCs "
                "of the 'Alone IPC' tab." % (", ".join(e.name for e in exps), "; ".join(pname(p) for p in pairs)),
                min(len(mc), 14))
    for i, (k, (h, w, f)) in enumerate(mc.items(), 1):
        hdr_cell(wsM, HDR_ROW, i, h, F_TITLE if i <= 6 else F_SUB, w)
    wsM.row_dimensions[HDR_ROW].height = 64
    mm_row = {}
    short = lambda t: t.split("-")[0]
    for i, mn in enumerate(common):
        r = FIRST + i
        mm_row[mn] = r
        k0, k1 = cc_row[(mn, 0)], cc_row[(mn, NC - 1)]
        kr = lambda col_: "'Core Comparison'!%s%d:%s%d" % (col_, k0, col_, k1)
        F = {"id": meta[mn]["id"], "name": mn, "type": meta[mn]["type"], "n_hc": meta[mn]["n_hc"], "n_hp": meta[mn]["n_hp"],
             "bench": " | ".join(short(t) for t in base.logs[mn].traces)}
        for x, y in pairs:
            sp = kr(ccol["sp_%s_%s" % (x, y)])
            sfx = "%s_%s" % (x, y)
            F["wsrel_" + sfx] = "=AVERAGE(%s)" % sp
            F["hsrel_" + sfx] = "=%d/SUMPRODUCT(1/%s)" % (NC, sp)
            F["minsp_" + sfx] = "=MIN(%s)" % sp
            F["maxsp_" + sfx] = "=MAX(%s)" % sp
        for e in exps:
            t, mp, mr = e.tag, e.mpos, e.mrow[mn]
            qm = q(e.name + "_mix")
            ra_ = lambda key, e=e: e.rng(key, mn)
            F["thr_" + t] = "=SUM(%s)" % ra_("ipc")
            F["llcl_" + t] = "=SUM(%s)" % ra_("LLC_load_miss")
            F["llct_" + t] = "=SUM(%s)" % ra_("LLC_total_miss")
            F["l2cl_" + t] = "=SUM(%s)" % ra_("L2C_load_miss")
            F["mpki_" + t] = "=%s%d/SUM(%s)*1000" % (mcol["llcl_" + t], r, ra_("instructions"))
            F["lat_" + t] = "=IFERROR(SUMPRODUCT(%s,%s)/SUM(%s),\"\")" % (ra_("LLC_average_miss_latency"), ra_("LLC_total_miss"), ra_("LLC_total_miss"))
            F["l2cmpki_" + t] = "=SUM(%s)/SUM(%s)*1000" % (ra_("L2C_load_miss"), ra_("instructions"))
            F["llchit_" + t] = "=IFERROR(SUM(%s)/SUM(%s),\"\")" % (ra_("LLC_total_hit"), ra_("LLC_total_access"))
            if e.has_pf:
                F["pfacc_" + t] = "=IFERROR((SUM(%s)+SUM(%s))/(SUM(%s)+SUM(%s)+SUM(%s)+SUM(%s)),\"\")" % (
                    ra_("L1D_prefetch_useful"), ra_("L2C_prefetch_useful"), ra_("L1D_prefetch_useful"), ra_("L1D_prefetch_useless"),
                    ra_("L2C_prefetch_useful"), ra_("L2C_prefetch_useless"))
                F["pfpki_" + t] = "=IFERROR((SUM(%s)+SUM(%s))/SUM(%s)*1000,\"\")" % (ra_("L1D_prefetch_issued"), ra_("L2C_prefetch_issued"), ra_("instructions"))
            if e.has_occ:
                F["occ_" + t] = "=SUM(%s)" % ra_("llc_occ_share")
                F["occpf_" + t] = "=SUM(%s)" % ra_("llc_occ_pf")
                F["occmax_" + t] = "=MAX(%s)" % ra_("llc_occ_share")
                F["occmin_" + t] = "=MIN(%s)" % ra_("llc_occ_share")
            F["dram_" + t] = "=%s!%s%d" % (qm, mp["DRAM read requests (RQ hit+miss)"], mr)
            F["rbh_" + t] = "=%s!%s%d" % (qm, mp["DRAM RQ row-buffer hit rate"], mr)
            F["bw_" + t] = "=%s!%s%d" % (qm, mp["% of bandwidth epochs in the top-2 levels"], mr)
            F["t_" + t] = "=%s!%s%d" % (qm, mp["Sim time (min)"], mr)
            if e.has_ucp:
                cells = ["%s!%s%d" % (qm, mp["UCP quota core%d" % c], mr) for c in range(NC)]
                F["q_" + t] = "=IF(COUNT(%s)=%d,%s,\"\")" % (",".join(cells), NC, "&\"/\"&".join(cells))
            if have_alone:
                F["ws_" + t] = "=IF(COUNT(%s)=%d,SUM(%s),\"\")" % (ra_("norm"), NC, ra_("norm"))
                F["hs_" + t] = "=IF(COUNT(%s)=%d,%d/SUM(%s),\"\")" % (ra_("slow"), NC, NC, ra_("slow"))
                F["fair_" + t] = "=IF(COUNT(%s)=%d,MIN(%s)/MAX(%s),\"\")" % (ra_("norm"), NC, ra_("norm"), ra_("norm"))
                F["msd_" + t] = "=IF(COUNT(%s)=%d,MAX(%s),\"\")" % (ra_("slow"), NC, ra_("slow"))
        for key, (h, w, fmt) in mc.items():
            if key in F:
                cell = wsM[mcol[key] + str(r)]
                cell.value, cell.font, cell.number_format = F[key], fnt(), fmt
    mm_last = FIRST + len(common) - 1
    wsM.freeze_panes = "D6"
    if not have_alone:
        for k in mc:
            if re.match(r"(ws|hs|msd|fair)_[a-g]$", k):
                wsM.column_dimensions[mcol[k]].hidden = True

    # ---------------- Comparison tab: plot first, table below, one block per metric
    # key = suffix of the per-experiment columns of the 'Mix Comparison' tab (key_<tag>);  rel = metric expressed against the
    # baseline experiment (baseline = 1.0);  need_pf = only experiments with an L2C prefetcher;  info = no 'improvement' columns
    blocks = [
        dict(key="thr", sec="Performance", short="Total IPC", title="Total IPC (sum of the IPC of the 4 cores)",
             hib=True, scale=1, fmt="0.000", unit="IPC", note="higher is better"),
        dict(key="ws", sec="Performance", short="Weighted speedup", title="Weighted speedup (sum over cores of IPC / IPC alone)",
             hib=True, scale=1, fmt="0.000", unit="weighted speedup", note="higher is better", need_alone=True),
        dict(key="wsrel", sec="Performance", short="Relative speedup", rel="wsrel",
             title="Relative weighted speedup ({A} = 1.0): mean over cores of IPC / IPC({A})",
             hib=True, scale=1, fmt="0.000", unit="relative weighted speedup", note="higher is better; needs no single-core data"),
        dict(key="hs", sec="Performance", short="Harmonic speedup", title="Harmonic speedup (fairness-aware throughput)",
             hib=True, scale=1, fmt="0.000", unit="harmonic speedup", note="higher is better", need_alone=True),
        dict(key="minsp", sec="Performance", short="Worst-core speed-up", rel="minsp",
             title="Worst-case core: lowest per-core IPC speed-up in the mix ({A} = 1.0)",
             hib=True, scale=1, fmt="0.000", unit="lowest per-core speed-up", note="below 1.0 = at least one core is SLOWER than under {A}"),
        dict(key="msd", sec="Fairness", short="Max slowdown", title="Maximum slowdown (max over cores of IPC alone / IPC shared)",
             hib=False, scale=1, fmt="0.000", unit="max slowdown", note="lower is better; 1.0 = no core slowed down by sharing", need_alone=True),
        dict(key="fair", sec="Fairness", short="Fairness", title="Fairness (min / max normalised IPC over the 4 cores)",
             hib=True, scale=1, fmt="0.000", unit="fairness index", note="higher is better; 1.0 = all cores are slowed down equally", need_alone=True),
        dict(key="llcl", sec="Caches", short="LLC load misses", title="LLC load misses (millions)",
             hib=False, scale=1e6, fmt="0.000", unit="millions of misses", note="lower is better - improvement = reduction"),
        dict(key="llct", sec="Caches", short="LLC total misses", title="LLC total misses incl. prefetch / RFO / writeback (millions)",
             hib=False, scale=1e6, fmt="0.000", unit="millions of misses", note="lower is better - improvement = reduction"),
        dict(key="l2cl", sec="Caches", short="L2C load misses", title="L2C load misses (millions)",
             hib=False, scale=1e6, fmt="0.000", unit="millions of misses", note="lower is better - improvement = reduction"),
        dict(key="mpki", sec="Caches", short="LLC load MPKI", title="LLC load misses per 1000 instructions (MPKI)",
             hib=False, scale=1, fmt="0.00", unit="MPKI", note="lower is better"),
        dict(key="l2cmpki", sec="Caches", short="L2C load MPKI", title="L2C load misses per 1000 instructions (MPKI)",
             hib=False, scale=1, fmt="0.00", unit="MPKI", note="lower is better"),
        dict(key="llchit", sec="Caches", short="LLC hit rate", title="LLC hit rate (hits / accesses, all request types)",
             hib=True, scale=1, fmt="0.0%", unit="hit rate", note="higher is better"),
        dict(key="lat", sec="Caches", short="LLC miss latency", title="Average LLC miss latency (cycles, weighted by misses)",
             hib=False, scale=1, fmt="0.0", unit="cycles", note="lower is better - improvement = reduction"),
        dict(key="pfacc", sec="Prefetcher", short="Prefetch accuracy", title="Prefetch accuracy (L1D+L2C) = useful / (useful + useless)",
             hib=True, scale=1, fmt="0.0%", unit="accuracy", note="higher is better; only experiments with a prefetcher", need_pf=True),
        dict(key="pfpki", sec="Prefetcher", short="Prefetches PKI", title="L1D+L2C prefetches issued per 1000 instructions (aggressiveness)",
             hib=True, scale=1, fmt="0.00", unit="prefetches / kilo-instruction", note="informational: no 'improvement' is defined",
             need_pf=True, info=True),
        dict(key="dram", sec="Memory", short="DRAM reads", title="DRAM read requests (millions) = memory traffic",
             hib=False, scale=1e6, fmt="0.000", unit="millions of requests",
             note="lower is better - prefetching usually ADDS traffic, so a negative improvement is expected"),
        dict(key="rbh", sec="Memory", short="DRAM row-buffer hits", title="DRAM read row-buffer hit rate",
             hib=True, scale=1, fmt="0.0%", unit="row-buffer hit rate", note="higher is better"),
        dict(key="bw", sec="Memory", short="Bandwidth pressure", title="Share of DRAM-bandwidth epochs in the two highest utilisation levels",
             hib=False, scale=1, fmt="0.0%", unit="share of epochs", note="lower = less bandwidth pressure (only simulators that report bandwidth epochs)",
             need_bw=True),
    ]
    occ_exps = [e for e in exps if e.has_occ]
    if occ_exps:
        occ_blocks = [
            dict(key="occ", sec="LLC occupancy", short="Total occupancy", title="LLC occupancy of all 4 cores together (valid blocks, % of the LLC)",
                 hib=True, scale=1, fmt="0.0%", unit="share of the LLC", note="informational: 100% = LLC completely filled", need_occ=True, info=True),
            dict(key="occmax", sec="LLC occupancy", short="Largest core share", title="Largest share of the LLC held by a single core",
                 hib=True, scale=1, fmt="0.0%", unit="share of the LLC", note="informational: how dominant the most LLC-hungry core is",
                 need_occ=True, info=True),
            dict(key="occmin", sec="LLC occupancy", short="Smallest core share", title="Smallest share of the LLC held by a single core",
                 hib=True, scale=1, fmt="0.0%", unit="share of the LLC", note="informational: how squeezed the most starved core is",
                 need_occ=True, info=True),
            dict(key="occpf", sec="LLC occupancy", short="Unused prefetches", title="LLC share held by prefetched blocks that were not used yet (all cores)",
                 hib=False, scale=1, fmt="0.0%", unit="share of the LLC", note="informational: prefetch pollution proxy",
                 need_occ=True, need_pf=True, info=True)]
        occ_blocks += [dict(kind="stack", key="occstack_" + e.tag, tag=e.tag, sec="LLC occupancy", short="Per core: " + e.name,
                            title="%s: share of the LLC held by each core (stacked; the full bar = all cores together)" % e.name,
                            hib=True, scale=1, fmt="0.0%", unit="share of the LLC", note="core numbers are the core slots of the mix", need_occ=True)
                       for e in occ_exps]
        occ_blocks += [dict(kind="core", key="occcore%d" % k, k=k, sec="LLC occupancy", short="Core %d share" % k,
                            title="Core %d: share of the LLC it occupies" % k, hib=True, scale=1, fmt="0.0%", unit="share of the LLC",
                            note="informational; compare the experiments for the same core slot", need_occ=True, info=True) for k in range(NC)]
        ix = next((i for i, b_ in enumerate(blocks) if b_["sec"] == "Prefetcher"), len(blocks))
        blocks = blocks[:ix] + occ_blocks + blocks[ix:]
    block_ok = lambda b_, e: (not b_.get("need_pf") or e.has_pf) and (not b_.get("need_occ") or e.has_occ) and (not b_.get("need_bw") or e.has_bw)
    blocks = [b_ for b_ in blocks if (have_alone or not b_.get("need_alone")) and any(block_ok(b_, e) for e in exps)]
    N = len(common)
    title_block(wsC, "%s - per-mix comparison, %d four-core mixes" % (" vs ".join(e.name for e in exps), N),
                "For every metric: the plot shows all mixes (x axis = Mix ID; %s); the table under the plot gives the "
                "per-mix values and the %% improvement of %s. Improvement is positive when the FIRST experiment of a pair is BETTER "
                "(higher IPC/speedup, or fewer misses/lower latency/less traffic). Blocks are grouped: Performance, Fairness, Caches, "
                "Prefetcher, Memory. Single-core (per trace / per class) views: tab 'Single-Core View'; by mix group: tab 'Group Plots'."
                % (", ".join("%s = %s" % (COLOR_NAMES[i], e.name) for i, e in enumerate(exps)),
                   "; ".join(pname(p) for p in pairs)), 14)
    for i, w in enumerate([9, 40, 20] + [17] * 12, 1):
        wsC.column_dimensions[CL(i)].width = w
    start = 3 + (len(blocks) + 7) // 8 + 1               # leave room for the 'Jump to' rows
    tabl, links = {}, []
    for bi, bl in enumerate(blocks):
        incl = [e for e in exps if block_ok(bl, e)]
        tags = [e.tag for e in incl]
        sc = "" if bl["scale"] == 1 else "/%d" % int(bl["scale"])
        series, extra, stacked = [], [], False
        if bl.get("kind") == "stack":          # one bar per mix, split into the 4 cores
            tg = bl["tag"]
            incl, tags = [E[tg]], [tg]
            occ_c = ccol["occ_share_" + tg]
            series = [dict(name="Core %d" % k, fmt="0.0%", color=CORE_COLS[k],
                           cell=(lambda r, i, k=k, c_=occ_c: "='Core Comparison'!%s%d" % (c_, cc_row[(common[i], k)]))) for k in range(NC)]
            extra = [dict(name="Total (all cores)", fmt="0.0%",
                          cell=(lambda r, i, c_=occ_c: "=SUM('Core Comparison'!%s%d:%s%d)" % (c_, cc_row[(common[i], 0)], c_, cc_row[(common[i], NC - 1)])))]
            stacked = True
        elif bl.get("kind") == "core":         # one core slot, the experiments side by side
            series = [dict(name=e.name, fmt="0.0%", color=EXP_COL[e.tag],
                           cell=(lambda r, i, k=bl["k"], c_=ccol["occ_share_" + e.tag]: "='Core Comparison'!%s%d" % (c_, cc_row[(common[i], k)])))
                      for e in incl]
        for e in ([] if series else incl):
            if bl.get("rel"):
                key = None if e.tag == base.tag else "%s_%s_%s" % (bl["rel"], e.tag, base.tag)
            else:
                key = "%s_%s" % (bl["key"], e.tag)
            series.append(dict(name=e.name, fmt=bl["fmt"], color=EXP_COL[e.tag],
                               cell=(lambda r, i, key=key, sc=sc: 1 if key is None else
                                     "=IF('Mix Comparison'!%s%d=\"\",\"\",'Mix Comparison'!%s%d%s)"
                                     % (mcol[key], mm_row[common[i]], mcol[key], mm_row[common[i]], sc))))
        bpairs = [] if bl.get("info") else [p for p in pairs if p[0] in tags and p[1] in tags]
        imps = [dict(name="%% improvement: %s" % pname(p), x=tags.index(p[0]), y=tags.index(p[1]), hib=bl["hib"]) for p in bpairs]
        title = ("%d. %s - %s   [%s]" % (bi + 1, bl["sec"], bl["title"], bl["note"])).replace("{A}", A)
        lead = lambda r, i: ["='Mix Comparison'!%s%d" % (mcol[k], mm_row[common[i]]) for k in ("id", "name", "type")]
        cfmt = "0.0#" if (bl["fmt"] == "0.000" and bl["scale"] == 1) else bl["fmt"]
        info = emit_block(wsC, start, title, ["Mix ID", "Mix", "Type"], N, lead, series, imps,
                          bl["unit"], cfmt, avg_label="Average over mixes", stacked=stacked, extra=extra)
        if not bl.get("kind"):
            tabl[bl["key"]] = dict(info, pairs=bpairs, tags=tags, title=bl["title"].replace("{A}", A),
                                   vcol={t: info["vcol"][j] for j, t in enumerate(tags)},
                                   icol={p: info["icol"][j] for j, p in enumerate(bpairs)})
        links.append((bl["short"], start))
        start = info["next"]
    write_index(wsC, 3, links)
    wsC.freeze_panes = "A%d" % (3 + (len(blocks) + 7) // 8)

    # ---------------- Summary tab
    n = len(exps)
    wsS.column_dimensions["A"].width = 44
    for c in range(2, 20):
        wsS.column_dimensions[CL(c)].width = 15
    title_block(wsS, "Summary - %s" % " vs ".join(e.name for e in exps),
                "Averages of the per-mix numbers of the Comparison tab. Improvement of a pair 'X over Y' is positive when X is better than Y. "
                "Pairs: %s." % "; ".join(pname(p) for p in pairs), 9)
    row = 4
    hdr_cell(wsS, row, 1, "Headline (average over %d mixes)" % N)
    heads = [e.name for e in exps] + ["%% improvement: %s" % pname(p) for p in pairs] \
        + ["Mixes improved: %s" % pname(p) for p in pairs] + ["Mixes degraded: %s" % pname(p) for p in pairs]
    for i, t in enumerate(heads, 2):
        hdr_cell(wsS, row, i, t, F_SUB)
    wsS.row_dimensions[row].height = 44
    row += 1
    T = tabl
    cr = lambda key, col_: "Comparison!$%s$%d:$%s$%d" % (col_, T[key]["t0"], col_, T[key]["t1"])
    head_first = row
    for bl in blocks:
        if bl.get("kind"):                      # per-core / stacked occupancy plots have no headline row
            continue
        k, tb = bl["key"], tabl[bl["key"]]
        wsS.cell(row, 1, tb["title"].split(":")[0].split(" (")[0]).font = fnt()
        for ei, e in enumerate(exps):
            if e.tag in tb["vcol"]:
                cell = wsS.cell(row, 2 + ei, "=IFERROR(AVERAGE(%s),\"\")" % cr(k, tb["vcol"][e.tag]))
                cell.font, cell.number_format = fnt(), bl["fmt"]
        for pi, (x, y) in enumerate(pairs):
            if (x, y) not in tb["icol"]:
                continue
            cx, cy = CL(2 + [e.tag for e in exps].index(x)), CL(2 + [e.tag for e in exps].index(y))
            cell = wsS.cell(row, 2 + n + pi, ("=IFERROR(%s%d/%s%d-1,\"\")" if bl["hib"] else "=IFERROR(1-%s%d/%s%d,\"\")") % (cx, row, cy, row))
            cell.font, cell.number_format = fnt(True), "0.00%"
            wsS.cell(row, 2 + n + len(pairs) + pi, "=COUNTIF(%s,\">0\")" % cr(k, tb["icol"][(x, y)])).font = fnt()
            wsS.cell(row, 2 + n + 2 * len(pairs) + pi, "=COUNTIF(%s,\"<0\")" % cr(k, tb["icol"][(x, y)])).font = fnt()
        row += 1
    cf_posneg(wsS, "%s%d:%s%d" % (CL(2 + n), head_first, CL(1 + n + len(pairs)), row - 1))
    wsS.cell(row, 1, "Geometric mean of the per-mix relative weighted speedup (%s = 1)" % A).font = fnt()
    for ei, e in enumerate(exps):
        if e.tag != base.tag:
            cell = wsS.cell(row, 2 + ei, "=IFERROR(GEOMEAN(%s),\"\")" % cr("wsrel", tabl["wsrel"]["vcol"][e.tag]))
            cell.font, cell.number_format = fnt(), "0.0000"
    row += 2

    def group_table(row, heading, crit_range_fn, crit_values, pair, crit_fmt=None):
        hdr_cell(wsS, row, 1, heading)
        cols_ = [("# mixes", None, "0"), ("Total IPC", "thr", "0.00%"), ("Relative weighted speedup", "wsrel", "0.00%")]
        if have_alone:
            cols_.insert(2, ("Weighted speedup", "ws", "0.00%"))
        cols_ += [("LLC load misses", "llcl", "0.00%"), ("L2C load misses", "l2cl", "0.00%"), ("DRAM reads", "dram", "0.00%")]
        for i, (t, _, _) in enumerate(cols_, 2):
            hdr_cell(wsS, row, i, t + (" improvement" if i > 2 else ""), F_SUB)
        wsS.row_dimensions[row].height = 30
        row += 1
        for v in crit_values:
            wsS.cell(row, 1, crit_fmt(v) if crit_fmt else v).font = fnt()
            crit = "$A%d" % row if not crit_fmt else str(v)
            for i, (t, k, fm) in enumerate(cols_, 2):
                rng_c = crit_range_fn()
                if k is None:
                    f = "=COUNTIF(%s,%s)" % (rng_c, crit)
                else:
                    f = "=IFERROR(AVERAGEIFS(%s,%s,%s),\"\")" % (cr(k, tabl[k]["icol"][pair]), rng_c, crit)
                cell = wsS.cell(row, i, f)
                cell.font, cell.number_format = fnt(), fm
            row += 1
        cf_posneg(wsS, "C%d:%s%d" % (row - len(crit_values), CL(len(cols_) + 1), row - 1))
        return row + 1

    types = sorted({meta[nn]["type"] for nn in common}, key=lambda t: min(meta[nn]["id"] for nn in common if meta[nn]["type"] == t))
    mm = lambda col_: "'Mix Comparison'!$%s$%d:$%s$%d" % (mcol[col_], FIRST, mcol[col_], mm_last)
    nps = sorted({meta[nn]["n_hp"] for nn in common if meta[nn]["n_hp"] is not None})
    nhc = sorted({meta[nn]["n_hc"] for nn in common if meta[nn]["n_hc"] is not None})
    kc = lambda col_: "'Core Comparison'!$%s$%d:$%s$%d" % (col_, FIRST, col_, kc_last)
    classes = [c for c in ("HCHP", "HCLP", "LCHP", "LCLP") if any(c == meta[nn]["classes"][i] for nn in common for i in range(NC))]
    for pi, (x, y) in enumerate(pairs):
        pr = (x, y)
        wsS.merge_cells(start_row=row, start_column=1, end_row=row, end_column=9)
        c0 = wsS.cell(row, 1, "Section %d: %s  (improvement of %s relative to %s)" % (pi + 1, pname(pr), nm[x], nm[y]))
        c0.font, c0.fill = fnt(True, 12, "FFFFFF"), F_TITLE
        wsS.row_dimensions[row].height = 22
        row += 2
        row = group_table(row, "By mix type", lambda: mm("type"), types, pr)
        if nps:
            row = group_table(row, "By number of High-prefetch cores in the mix", lambda: mm("n_hp"), nps, pr,
                              crit_fmt=lambda v: "%d High-prefetch core(s)" % v)
        if nhc:
            row = group_table(row, "By number of High-cache cores in the mix", lambda: mm("n_hc"), nhc, pr,
                              crit_fmt=lambda v: "%d High-cache core(s)" % v)
        # per-core by class
        pf_tags = [t for t in (x, y) if E[t].has_pf]
        hdr_cell(wsS, row, 1, "Per core, by trace class (all mixes)")
        heads = ["# cores", "Avg IPC speed-up", "Avg LLC load-miss reduction", "Avg L2C load-miss reduction"] \
            + ["Avg Prefetch accuracy (L1D+L2C) (%s)" % nm[t] for t in pf_tags]
        for i, t in enumerate(heads, 2):
            hdr_cell(wsS, row, i, t, F_SUB)
        wsS.row_dimensions[row].height = 42
        row += 1
        for cls in classes:
            wsS.cell(row, 1, cls).font = fnt()
            f = ["=COUNTIF(%s,$A%d)" % (kc(ccol["cls"]), row)]
            for key in ["sp_%s_%s" % pr, "llcl_red_%s_%s" % pr, "l2cl_red_%s_%s" % pr] + ["pfacc_" + t for t in pf_tags]:
                f.append("=IFERROR(AVERAGEIFS(%s,%s,$A%d),\"\")" % (kc(ccol[key]), kc(ccol["cls"]), row))
            for i, ff in enumerate(f, 2):
                cell = wsS.cell(row, i, ff)
                cell.font = fnt()
                cell.number_format = "0" if i == 2 else "0.0000" if i == 3 else "0.00%"
            row += 1
        row += 2
    wsS.cell(row, 1, "HCHP = High-cache/High-prefetch, HCLP = High-cache/Low-prefetch, LCHP = Low-cache/High-prefetch, "
                     "LCLP = Low-cache/Low-prefetch (classes of the single-core characterization).").font = fnt(italic=True, size=9, color="595959")

    # ---------------- Single-Core View: per trace and per trace class (the single-core metrics)
    tr_cls = {}
    for mn in common:
        for core in range(NC):
            tr_cls.setdefault(base.logs[mn].traces[core], meta[mn]["classes"][core])
    cls_order = {c: i for i, c in enumerate(("HCHP", "HCLP", "LCHP", "LCLP"))}
    traces_sorted = sorted(tr_cls, key=lambda t: (cls_order.get(tr_cls[t], 9), t))
    NT = len(traces_sorted)
    pf_exps = [e for e in exps if e.has_pf]
    ex_series = lambda es, key, fmt: [dict(name=e.name, fmt=fmt, color=EXP_COL[e.tag], cell=key(e)) for e in es]
    pair_series = lambda key, fmt: [dict(name="%s / %s" % (nm[x], nm[y]), fmt=fmt, color=PAIR_COLS[i], cell=key((x, y)))
                                    for i, (x, y) in enumerate(pairs)]
    pair_imps = lambda es, hib: [dict(name="%% improvement: %s" % pname(p), x=[e.tag for e in es].index(p[0]),
                                      y=[e.tag for e in es].index(p[1]), hib=hib)
                                 for p in pairs if p[0] in [e.tag for e in es] and p[1] in [e.tag for e in es]]
    title_block(wsV, "Single-core view - per trace and per trace class, %s" % " vs ".join(e.name for e in exps),
                "Every number is the average over all cores that ran that trace (or trace class) in the %d mixes, taken from the "
                "'Core Comparison' tab. 'IPC alone' = the single-core run of the trace (tab 'Alone IPC'); normalised IPC = IPC in the "
                "4-core mix / IPC alone (1.0 = no slowdown from sharing the LLC and DRAM). Classes: HCHP = High-cache/High-prefetch, "
                "HCLP = High-cache/Low-prefetch, LCHP = Low-cache/High-prefetch, LCLP = Low-cache/Low-prefetch." % N, 14)
    wsV.column_dimensions["A"].width = 26
    wsV.column_dimensions["B"].width = 10
    wsV.column_dimensions["C"].width = 14
    for i in range(4, 16):
        wsV.column_dimensions[CL(i)].width = 17
    classes_v = [c for c in ("HCHP", "HCLP", "LCHP", "LCLP") if c in set(tr_cls.values())] or ["(unclassified)"]
    avg_by = lambda key, by: (lambda r, i: "=IFERROR(AVERAGEIFS(%s,%s,$A%d),\"\")" % (kc(ccol[key]), kc(ccol[by]), r))
    blocks_v, links_v = [], []
    # --- by trace class (4 rows)
    cls_lead = lambda r, i: [classes_v[i]]
    blocks_v += [
        ("By class - average IPC speed-up of a core (X / Y)", ["Trace class"], len(classes_v), cls_lead,
         pair_series(lambda p: avg_by("sp_%s_%s" % p, "cls"), "0.000"), [], "IPC speed-up", "0.00", None, False, "Class: IPC speed-up"),
        ("By class - average LLC load-miss reduction of a core", ["Trace class"], len(classes_v), cls_lead,
         pair_series(lambda p: avg_by("llcl_red_%s_%s" % p, "cls"), "0.0%"), [], "load-miss reduction", "0%", None, False, "Class: LLC miss red."),
        ("By class - average L2C load-miss reduction of a core", ["Trace class"], len(classes_v), cls_lead,
         pair_series(lambda p: avg_by("l2cl_red_%s_%s" % p, "cls"), "0.0%"), [], "load-miss reduction", "0%", None, False, "Class: L2C miss red."),
    ]
    if have_alone:
        blocks_v.append(("By class - average normalised IPC (IPC in the mix / IPC alone)", ["Trace class"], len(classes_v), cls_lead,
                         ex_series(exps, lambda e: avg_by("norm_" + e.tag, "cls"), "0.000"), pair_imps(exps, True),
                         "normalised IPC", "0.0#", None, False, "Class: norm. IPC"))
    blocks_v.append(("By class - average LLC load MPKI of a core", ["Trace class"], len(classes_v), cls_lead,
                     ex_series(exps, lambda e: avg_by("llc_load_mpki_" + e.tag, "cls"), "0.00"), pair_imps(exps, False),
                     "MPKI", "0.0", None, False, "Class: LLC MPKI"))
    if pf_exps:
        blocks_v.append(("By class - average Prefetch accuracy (L1D+L2C)", ["Trace class"], len(classes_v), cls_lead,
                         ex_series(pf_exps, lambda e: avg_by("pfacc_" + e.tag, "cls"), "0.0%"), pair_imps(pf_exps, True),
                         "accuracy", "0%", None, False, "Class: pf accuracy"))
    occ_exps = [e for e in exps if e.has_occ]
    occpf_exps = [e for e in occ_exps if e.has_pf]
    if occ_exps:
        blocks_v.append(("By class - average share of the LLC held by a core", ["Trace class"], len(classes_v), cls_lead,
                         ex_series(occ_exps, lambda e: avg_by("occ_share_" + e.tag, "cls"), "0.0%"), [],
                         "share of the LLC", "0%", None, False, "Class: LLC occupancy"))
    if occpf_exps:
        blocks_v.append(("By class - average LLC share held by unused prefetched blocks of a core", ["Trace class"], len(classes_v), cls_lead,
                         ex_series(occpf_exps, lambda e: avg_by("occpf_" + e.tag, "cls"), "0.0%"), [],
                         "share of the LLC", "0%", None, False, "Class: unused prefetches"))
    # --- by trace (one row per trace)
    tr_lead = lambda r, i: [traces_sorted[i], tr_cls[traces_sorted[i]], "=COUNTIF(%s,$A%d)" % (kc(ccol["trace"]), r)]
    TL = ["Trace", "Class", "# cores"]
    alone_series = dict(name="IPC alone (single-core)", fmt="0.000", color=ALONE_COL,
                        cell=lambda r, i: "=IFERROR(INDEX('Alone IPC'!$B:$B,MATCH($A%d,'Alone IPC'!$A:$A,0)),\"\")" % r)
    ipc_ser = ([alone_series] if have_alone else []) + ex_series(exps, lambda e: avg_by("ipc_" + e.tag, "trace"), "0.000")
    off = 1 if have_alone else 0
    ipc_imps = [dict(i_, x=i_["x"] + off, y=i_["y"] + off) for i_ in pair_imps(exps, True)]
    blocks_v += [
        ("By trace - IPC: single-core alone vs in the 4-core mixes" if have_alone else "By trace - average IPC in the 4-core mixes",
         TL, NT, tr_lead, ipc_ser, ipc_imps, "IPC", "0.0#", "Average over traces", True, "Trace: IPC"),
    ]
    if have_alone:
        blocks_v.append(("By trace - normalised IPC (IPC in the mix / IPC alone; 1.0 = unaffected by sharing)", TL, NT, tr_lead,
                         ex_series(exps, lambda e: avg_by("norm_" + e.tag, "trace"), "0.000"), pair_imps(exps, True),
                         "normalised IPC", "0.0#", "Average over traces", True, "Trace: norm. IPC"))
    blocks_v += [
        ("By trace - IPC speed-up of a core (X / Y, 1.0 = no change)", TL, NT, tr_lead,
         pair_series(lambda p: avg_by("sp_%s_%s" % p, "trace"), "0.000"), [], "IPC speed-up", "0.0#",
         "Average over traces", True, "Trace: IPC speed-up"),
        ("By trace - LLC load MPKI", TL, NT, tr_lead, ex_series(exps, lambda e: avg_by("llc_load_mpki_" + e.tag, "trace"), "0.00"),
         pair_imps(exps, False), "MPKI", "0.0", "Average over traces", True, "Trace: LLC MPKI"),
        ("By trace - L2C load MPKI", TL, NT, tr_lead, ex_series(exps, lambda e: avg_by("l2c_load_mpki_" + e.tag, "trace"), "0.00"),
         pair_imps(exps, False), "MPKI", "0.0", "Average over traces", True, "Trace: L2C MPKI"),
        ("By trace - LLC hit rate (all request types)", TL, NT, tr_lead, ex_series(exps, lambda e: avg_by("llc_hitrate_" + e.tag, "trace"), "0.0%"),
         pair_imps(exps, True), "hit rate", "0%", "Average over traces", True, "Trace: LLC hit rate"),
        ("By trace - average LLC miss latency (cycles)", TL, NT, tr_lead,
         ex_series(exps, lambda e: avg_by("LLC_average_miss_latency_" + e.tag, "trace"), "0.0"), pair_imps(exps, False),
         "cycles", "0", "Average over traces", True, "Trace: miss latency"),
    ]
    if pf_exps:
        blocks_v += [
            ("By trace - Prefetch accuracy (L1D+L2C)", TL, NT, tr_lead, ex_series(pf_exps, lambda e: avg_by("pfacc_" + e.tag, "trace"), "0.0%"),
             pair_imps(pf_exps, True), "accuracy", "0%", "Average over traces", True, "Trace: pf accuracy"),
            ("By trace - L1D+L2C prefetches issued per 1000 instructions", TL, NT, tr_lead,
             ex_series(pf_exps, lambda e: avg_by("pfpki_" + e.tag, "trace"), "0.00"), [], "prefetches / kilo-instruction", "0.0",
             "Average over traces", True, "Trace: pf PKI"),
        ]
    if occ_exps:
        blocks_v.append(("By trace - average share of the LLC held by a core running the trace", TL, NT, tr_lead,
                         ex_series(occ_exps, lambda e: avg_by("occ_share_" + e.tag, "trace"), "0.0%"), [],
                         "share of the LLC", "0%", "Average over traces", True, "Trace: LLC occupancy"))
    if occpf_exps:
        blocks_v.append(("By trace - LLC share held by unused prefetched blocks of the core", TL, NT, tr_lead,
                         ex_series(occpf_exps, lambda e: avg_by("occpf_" + e.tag, "trace"), "0.0%"), [],
                         "share of the LLC", "0%", "Average over traces", True, "Trace: unused prefetches"))
    start = 3 + (len(blocks_v) + 7) // 8 + 1
    for bi, (ttl, lh, nrows, lcell, ser, imps_, unit, cfmt, avgl, rot, short_) in enumerate(blocks_v):
        info_ = emit_block(wsV, start, "%d. %s" % (bi + 1, ttl), lh, nrows, lcell, ser, imps_, unit, cfmt, avg_label=avgl, rotate=rot)
        links_v.append((short_, start))
        start = info_["next"]
    write_index(wsV, 3, links_v, start_col=4)
    wsV.freeze_panes = "A%d" % (3 + (len(blocks_v) + 7) // 8)

    # ---------------- Group Plots: average improvement per mix group (type / # high-prefetch / # high-cache cores)
    title_block(wsG, "Group plots - average improvement per group of mixes, %s" % " vs ".join(e.name for e in exps),
                "Bars = average %% improvement of each compared pair (X over Y; positive = X is better) over the mixes of the group; "
                "the pairs are %s. Averages are taken from the per-mix improvement columns of the 'Comparison' tab." %
                "; ".join(pname(p) for p in pairs), 14)
    wsG.column_dimensions["A"].width = 34
    for i in range(2, 12):
        wsG.column_dimensions[CL(i)].width = 17
    groupings = [("mix type", "type", types, lambda v: v, lambda r, v: "$A%d" % r),
                 ("number of High-prefetch cores", "n_hp", nps, lambda v: "%d High-prefetch core(s)" % v, lambda r, v: str(v)),
                 ("number of High-cache cores", "n_hc", nhc, lambda v: "%d High-cache core(s)" % v, lambda r, v: str(v))]
    gmetrics = [k_ for k_ in ("thr", "wsrel", "llcl", "dram") if k_ in tabl]
    gblocks, links_g = [], []
    for gname, gcol, gvals, glabel, gcrit in groupings:
        if not gvals:
            continue
        for k_ in gmetrics:
            gblocks.append((gname, gcol, gvals, glabel, gcrit, k_))
    start = 3 + (len(gblocks) + 7) // 8 + 1
    for bi, (gname, gcol, gvals, glabel, gcrit, k_) in enumerate(gblocks):
        tb = tabl[k_]
        short_title = tb["title"].split(" (")[0].split(":")[0]
        ser = [dict(name="%s over %s" % (nm[x], nm[y]), fmt="0.0%", color=PAIR_COLS[i],
                    cell=(lambda r, i_, x=x, y=y, gcol=gcol, gvals=gvals, gcrit=gcrit, tb=tb:
                          "=IFERROR(AVERAGEIFS(Comparison!$%s$%d:$%s$%d,%s,%s),\"\")"
                          % (tb["icol"][(x, y)], tb["t0"], tb["icol"][(x, y)], tb["t1"], mm(gcol), gcrit(r, gvals[i_]))))
               for i, (x, y) in enumerate(pairs)]
        lead = lambda r, i_, gvals=gvals, glabel=glabel, gcol=gcol, gcrit=gcrit: \
            [glabel(gvals[i_]), "=COUNTIF(%s,%s)" % (mm(gcol), gcrit(r, gvals[i_]))]
        info_ = emit_block(wsG, start, "%d. %s - improvement by %s" % (bi + 1, short_title, gname), [gname.capitalize(), "# mixes"],
                           len(gvals), lead, ser, [], "average % improvement", "0%", band_cols=10)
        links_g.append(("%s / %s" % ({"thr": "Total IPC", "wsrel": "Rel. speedup", "llcl": "LLC misses", "dram": "DRAM reads"}[k_],
                                     "type" if gcol == "type" else gcol[2:].upper()), start))
        start = info_["next"]
    write_index(wsG, 3, links_g)
    wsG.freeze_panes = "A%d" % (3 + (len(gblocks) + 7) // 8)

    # ---------------- Core Plots: every core-level metric, per core and per mix, all experiments side by side
    occ_x = [e for e in exps if e.has_occ]
    # (key on the Core Comparison tab, title, number format, unit, higher-is-better or None = informational, experiments that have it)
    cmet = [("ipc", "IPC of the core", "0.000", "IPC", True, exps)]
    if have_alone:
        cmet += [("norm", "Normalised IPC (IPC in the mix / IPC alone; 1.0 = no slowdown from sharing)", "0.000", "normalised IPC", True, exps),
                 ("slow", "Slowdown (IPC alone / IPC in the mix)", "0.00", "slowdown", False, exps)]
    cmet += [("llc_load_mpki", "LLC load MPKI of the core", "0.00", "MPKI", False, exps),
             ("l2c_load_mpki", "L2C load MPKI of the core", "0.00", "MPKI", False, exps),
             ("LLC_load_miss", "LLC load misses of the core", "#,##0", "misses", False, exps),
             ("LLC_total_miss", "LLC total misses of the core (all request types)", "#,##0", "misses", False, exps),
             ("L2C_load_miss", "L2C load misses of the core", "#,##0", "misses", False, exps),
             ("LLC_average_miss_latency", "LLC average miss latency of the core (cycles)", "0.0", "cycles", False, exps),
             ("llc_hitrate", "LLC hit rate of the core (all request types)", "0.0%", "hit rate", True, exps)]
    cmet += [("pfacc", "Prefetch accuracy (L1D+L2C) of the core", "0.0%", "accuracy", True, pf_exps),
             ("pfpki", "L1D+L2C prefetches issued per 1000 instructions", "0.00", "prefetches / kilo-instruction", None, pf_exps)] if pf_exps else []
    cmet += [("occ_share", "LLC occupancy of the core (share of the LLC)", "0.0%", "share of the LLC", None, occ_x),
             ("occ_ways", "LLC occupancy of the core (average ways per set)", "0.00", "ways", None, occ_x),
             ("occpf", "Unused prefetched blocks of the core (share of the LLC)", "0.0%", "share of the LLC", None, [e for e in occ_x if e.has_pf])
             ] if occ_x else []
    if any(e.has_ucp for e in exps):
        cmet.append(("quota", "UCP quota of the core (ways at the end of the run)", "0", "ways", None, [e for e in exps if e.has_ucp]))
    cmet = [m_ for m_ in cmet if m_[5]]
    mm_name = "'Mix Comparison'!$%s$%d:$%s$%d" % (mcol["name"], FIRST, mcol["name"], FIRST + len(common) - 1)
    KC = lambda key: "'Core Comparison'!$%s:$%s" % (ccol[key], ccol[key])

    # ---- tab 1: pick a mix -> its 4 cores, every metric, experiments side by side (live formulas)
    title_block(wsP, "Core Plots - per core of ONE mix (choose the mix in cell B4), %s" % " vs ".join(e.name for e in exps),
                "Pick a mix in the yellow cell: every plot below shows its 4 cores (core slot : trace) with one bar per experiment, "
                "and the table under the plot gives the values and the % improvement of each pair. All plots update when another mix is chosen. "
                "The plots of all 50 mixes at once are in the tab 'Core Plots (all)'.", 14)
    wsP.column_dimensions["A"].width = 42
    wsP.column_dimensions["B"].width = 18
    for i in range(3, 16):
        wsP.column_dimensions[CL(i)].width = 19
    wsP["A4"] = "Select the mix:"
    wsP["A4"].font = fnt(True, 11)
    wsP["B4"] = common[0]
    wsP["B4"].font, wsP["B4"].fill = Font(name=FONT, size=11, bold=True, color="0000FF"), PatternFill("solid", fgColor="FFF2CC")
    wsP.merge_cells("B4:D4")
    dv = DataValidation(type="list", formula1="=%s" % mm_name, allow_blank=False)
    wsP.add_data_validation(dv)
    dv.add("B4")
    wsP["A5"] = "Mix type:"
    wsP["B5"] = "=IFERROR(INDEX('Core Comparison'!$%s:$%s,$B$6),\"\")" % (ccol["type"], ccol["type"])
    wsP["A6"] = "First row of the mix in 'Core Comparison':"
    wsP["B6"] = "=MATCH($B$4,'Core Comparison'!$%s:$%s,0)" % (ccol["mix"], ccol["mix"])
    for a in ("A5", "A6", "B5", "B6"):
        wsP[a].font = fnt(a[0] == "A", 10)
    lead_p = lambda r, i: ["=\"Core \"&INDEX(%s,$B$6+%d)&\": \"&INDEX(%s,$B$6+%d)" % (KC("core"), i, KC("trace"), i),
                          "=INDEX(%s,$B$6+%d)" % (KC("cls"), i)]
    all_lead = lambda r, i: ["%s c%d" % (common[i // NC].split("_")[0], i % NC), common[i // NC], i % NC,
                             "='Core Comparison'!%s%d" % (ccol["trace"], cc_row[(common[i // NC], i % NC)]),
                             "='Core Comparison'!%s%d" % (ccol["cls"], cc_row[(common[i // NC], i % NC)])]
    def core_blocks(ws, start, n_rows, lead_h, lead_f, cellf, avg, rot, max_w):
        links = []
        first = start
        specs = []
        for bi, (key, ttl, fmt, unit, hib, es) in enumerate(cmet):
            specs.append((bi, key, ttl, fmt, unit, hib, es))
        nrow_idx = (len(specs) + 7) // 8
        start += nrow_idx + 1
        for bi, key, ttl, fmt, unit, hib, es in specs:
            ser = [dict(name=e.name, fmt=fmt, color=EXP_COL[e.tag], cell=cellf(ccol["%s_%s" % (key, e.tag)])) for e in es]
            imps_ = pair_imps(es, hib) if hib is not None else []
            info_ = emit_block(ws, start, "%d. %s" % (bi + 1, ttl), lead_h, n_rows, lead_f, ser, imps_, unit, fmt,
                               avg_label=avg, rotate=rot, max_w=max_w)
            links.append((ttl.split(" (")[0].replace(" of the core", ""), start))
            start = info_["next"]
        write_index(ws, first, links, start_col=2)
        return start
    core_blocks(wsP, 8, NC, ["Core : trace", "Class"], lead_p,
                lambda col: (lambda r, i: "=IFERROR(INDEX('Core Comparison'!$%s:$%s,$B$6+%d),\"\")" % (col, col, i)),
                "Average of the 4 cores", False, 60)
    # ---- tab 2: all cores of all mixes
    title_block(wsA, "Core Plots (all) - every core of every mix, %s" % " vs ".join(e.name for e in exps),
                "One plot per metric with %d groups of bars (mix, core slot c0..c3), one bar per experiment in each group; the table under each plot has "
                "the values and the %% improvement of each pair per core. The plots are wide: scroll right, or zoom out. For a single mix use "
                "the tab 'Core Plots'." % (N * NC), 14)
    wsA.column_dimensions["A"].width = 12
    wsA.column_dimensions["B"].width = 30
    wsA.column_dimensions["C"].width = 7
    wsA.column_dimensions["D"].width = 22
    for i in range(5, 16):
        wsA.column_dimensions[CL(i)].width = 14
    core_blocks(wsA, 4, N * NC, ["Mix / core", "Mix", "Core", "Trace", "Class"], all_lead,
                lambda col: (lambda r, i: "='Core Comparison'!%s%d" % (col, cc_row[(common[i // NC], i % NC)])),
                "Average over all cores", True, 160)
    wsA.freeze_panes = "A4"

    # ---------------- Notes tab
    notes = [("Experiments", True)]
    for e in exps:
        notes.append(("%s: %s  (%d complete mixes) | binary: %s | LLC replacement: %s | L1D prefetcher: %s | L2C prefetcher: %s%s"
                      % (e.name, e.dir, len(e.logs), e.binary or "?", (e.repl or "?").upper(), e.l1d or "?", e.l2c or "?",
                         " | UCP partitioning" if e.has_ucp else ""), False))
    notes += [
        ("Mixes compared: %d.  Setup found in the logs: %s cores, LLC %s sets x %s ways, warm-up %s, simulation %s instructions per core, %s DRAM channels."
         % ((len(common),) + s0), False),
        ("Compared pairs (X over Y, improvement positive when X is better): %s." % "; ".join(pname(p) for p in pairs), False),
        ("IPCP at the L2C is driven by the class / stride that the L1D part of IPCP sends with each prefetch (metadata).  Without an L1D "
         "prefetcher that metadata is 0, so the 'L2' only experiments (UCP_IPCP_L2, IPCP_L2) only issue IPCP's 'new IP -> next line' "
         "prefetch and are expected to stay close to their no-prefetcher baselines; the full IPCP is the 'L1_L2' experiments.", False),
        ("Pairs 'UCP_IPCP_X over IPCP_X' isolate what UCP partitioning adds on top of the same prefetcher; pairs '... over UCP' show what the "
         "prefetcher (with or without UCP) adds on top of UCP alone.", False),
    ]
    notes += [
        ("", False),
        ("Metric definitions", True),
        ("IPC = instructions / cycles of the core in its region of interest (computed from the log counters).", False),
        ("Total IPC (throughput) = sum of the 4 cores' IPC.", False),
        ("Per-core speed-up X/Y = IPC(X) / IPC(Y).  Relative weighted speedup of a pair = mean of the 4 per-core speed-ups.  "
         "In the Comparison / Summary tabs the 'relative weighted speedup' of every experiment is expressed against %s (%s itself = 1.0); the "
         "improvement of two experiments against each other is the ratio of those two numbers.  Relative harmonic speedup = 4 / sum(1/speed-up)." % (A, A), False),
        ("Weighted speedup = sum over the 4 cores of IPC_shared / IPC_alone.  Harmonic speedup = 4 / sum(IPC_alone / IPC_shared).  "
         "Max slowdown = max(IPC_alone / IPC_shared).", False),
        ("IPC_alone: %s. The same single-core IPC (no prefetcher, SRRIP, full LLC) is the denominator for all experiments, so the ratios "
         "between them are meaningful, but absolute values are not 'alone under UCP / IPCP'. Edit the experiment's column of the 'Alone IPC' tab to use "
         "another baseline for it." % (alone_note or "not available"), False),
        ("LLC / L2C load misses = demand load misses of the core (Core_n_LLC_load_miss, Core_n_L2C_load_miss). LLC total misses also include "
         "RFO, prefetch and writeback misses.", False),
        ("MPKI = misses per 1000 instructions.  Prefetch accuracy (L1D+L2C) = useful / (useful + useless) prefetches counted at the L1D and the L2C together.  "
         "Prefetches PKI = L1D + L1D+L2C prefetches issued per 1000 instructions (how aggressive the prefetcher is).  "
         "LLC hit rate = LLC hits / LLC accesses of all request types.", False),
        ("Normalised IPC = IPC in the 4-core mix / IPC alone (single-core run, 'Alone IPC' tab); slowdown = its inverse.  "
         "Max slowdown of a mix = largest slowdown of its 4 cores (lower is better).  Fairness = lowest / highest normalised IPC of the "
         "4 cores (1.0 = every core is slowed down equally).  Worst-case core speed-up = lowest per-core IPC speed-up in the mix, "
         "against the baseline experiment (below 1.0 = at least one core loses performance).", False),
        ("Core Plots tab: pick the mix in cell B4 (drop-down); the plots and tables read the 4 rows of that mix from 'Core Comparison'. "
         "Core Plots (all): all cores of all mixes (labels 'mixNN cK' = mix number, core slot K); one plot per metric.", False),
        ("Single-Core View tab: every number is the average over all cores that ran a trace (or a trace class) in the compared mixes, "
         "next to the single-core IPC of that trace run alone.  Group Plots tab: average improvement per mix type / per number of "
         "High-prefetch / High-cache cores.", False),
        ("DRAM read requests = row-buffer hits + misses of the read queues of all channels (memory traffic).  '% of bandwidth epochs in top-2 "
         "levels' = share of epochs spent in the two highest bandwidth-utilisation levels (only simulators that report bandwidth epochs).", False),
        ("% improvement of X over Y: higher-is-better metrics (IPC, speedups): X/Y - 1.  Lower-is-better metrics (misses, latency, DRAM reads): "
         "1 - X/Y, i.e. the reduction. Positive = X is better.", False),
        ("LLC occupancy (simulator instrumentation, read-only, added by run_4core_mixes_ipcp.py --build): the LLC (all cores share it) is scanned every "
         "N cycles from the end of the warm-up until the last core has finished; a valid block counts for the core that FILLED it (the block's cpu field). "
         "Occupancy share of a core = its average number of valid blocks / total LLC blocks; the sum over the 4 cores = occupancy of the whole LLC "
         "(100% = completely full). 'Ways' = share x associativity (average number of the 16 ways per set the core holds). Unused prefetches = blocks that "
         "were brought in by a prefetch and have not been hit since (prefetch bit still set): a proxy for prefetch pollution. In the 'Comparison' tab the "
         "stacked bars split the LLC per core; the part of the bar below 100% is the empty (invalid) part of the LLC.", False),
        ("Occupancy is NOT the UCP quota: the quota is the partition target (ways per core) set by the UCP policy, the occupancy is what the cores really "
         "hold, which can differ (a core may not fill its quota, blocks of an earlier partition remain until they are evicted, and prefetches/writebacks "
         "of a core count for that core). Occupancy columns exist only for logs of a binary built with the occupancy instrumentation; the other logs are "
         "left out of the occupancy plots.", False),
        ("UCP quotas = ways of the shared LLC per core at the end of the run ([UCP] line of the log); only UCP experiments have them.", False),
        ("Colours: blue = value parsed from a log, black = formula.", False),
        ("", False),
        ("Warnings", True),
    ] + [(w, False) for w in (warnings or ["none"])]
    wsN.column_dimensions["A"].width = 170
    nwarn = len(warnings)
    for i, (t, bold) in enumerate(notes, 1):
        c = wsN.cell(i, 1, t)
        c.font = fnt(bold, 12 if bold else 10, "1F4E78" if bold else ("C00000" if (nwarn and i > len(notes) - nwarn) else "000000"))
        c.alignment = Alignment(wrap_text=True, vertical="top")

    for w_ in (wsC, wsS, wsV, wsG, wsP, wsA, wsI, wsM, wsK):
        w_.page_setup.orientation = "landscape"
        w_.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        w_.page_setup.fitToWidth, w_.page_setup.fitToHeight = 1, 0
    wb.move_sheet("Notes", offset=8 - len(wb.sheetnames))      # Notes right after the plot tabs (Comparison, Summary, Single-Core View, Group Plots, Core Plots, Core Plots (all))
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(" | ".join("%s: %d complete mixes" % (e.name, len(e.logs)) for e in exps) + " | compared: %d" % len(common))
    for w in warnings:
        print("[WARN]", w)
    print("Written:", out.resolve())
    print("Open it in Excel (formulas are computed on opening; press Ctrl+Alt+F9 if cells look empty).")


if __name__ == "__main__":
    main()
