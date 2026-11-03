#!/usr/bin/env python3
"""
cd /home/svignesh/folder_e/champsim/ChampSim-master
pip install openpyxl            # only if not already installed
python3 scripts/make_characterization_excel.py
==============================================================================
Reads the ChampSim logs produced by scripts/run_char_workloads.sh
(results_250M/characterization/<trace>_way<W>_deg<D>.txt) and builds an Excel
workbook with the same layout as "IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx":

  1. IPC Surface         trace x (W{ways}_D{degree}) IPC grid
  2. Tacker Segregation  cache-benefit / prefetch-benefit classification
  3. IPC vs LLC Misses   ONE CHART PER TRACE (redesigned, see below)
  4. Raw Data            every value parsed from the logs (the only hard-coded
                         numbers in the workbook; everything else is formulas)

Chart design (tab 3), one per trace:
  * X axis, left -> right : LLC ways 1 -> 2 -> 4 -> 8 -> 16 -> 32
  * Bars (right axis)     : LLC misses (millions) at that way count. They fall
                            left -> right as capacity grows. Taken from the
                            baseline prefetch degree (default D0 = no prefetch)
                            so the bars show the pure effect of capacity.
  * Lines (left axis)     : IPC, bottom -> top, one line per prefetch degree.

Usage (run from anywhere):
    python3 scripts/make_characterization_excel.py
    python3 scripts/make_characterization_excel.py --logs-dir results_250M/characterization \
            --out IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx
    python3 scripts/make_characterization_excel.py --miss-metric load      # LLC LOAD misses only
    python3 scripts/make_characterization_excel.py --miss-metric total     # LLC TOTAL misses only

Requires:  pip install openpyxl
Partial sweeps are fine: missing (way, degree) points are left blank and the
affected traces are flagged "Incomplete" on the classification tab.
==============================================================================
"""
import argparse
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.chart.text import RichText, Text
from openpyxl.chart.title import Title
from openpyxl.drawing.text import (CharacterProperties, Paragraph,
                                   ParagraphProperties, RegularTextRun,
                                   RichTextProperties)
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.properties import CalcProperties

# --------------------------------------------------------------------------
# Defaults (match run_char_workloads.sh)
# --------------------------------------------------------------------------
DEFAULT_WAYS = [1, 2, 4, 8, 16, 32]
DEFAULT_DEGREES = [0, 1, 2, 4, 6, 8]
FONT = "Arial"

FILE_RE = re.compile(
    r"^(?P<trace>.+?)_way(?P<way>\d+)_deg(?P<deg>\d+)(?:[ _]*\(\d+\))?\.txt$")
IPC_RE = re.compile(
    r"^CPU\s+0\s+cumulative IPC:\s*(?P<ipc>[0-9.eE+-]+)\s+instructions:\s*(?P<ins>\d+)\s+cycles:\s*(?P<cyc>\d+)",
    re.M)
LLC_TOTAL_RE = re.compile(
    r"^LLC TOTAL\s+ACCESS:\s*(?P<acc>\d+)\s+HIT:\s*(?P<hit>\d+)\s+MISS:\s*(?P<miss>\d+)", re.M)
LLC_LOAD_RE = re.compile(
    r"^LLC LOAD\s+ACCESS:\s*(?P<acc>\d+)\s+HIT:\s*(?P<hit>\d+)\s+MISS:\s*(?P<miss>\d+)", re.M)
LLC_WAYS_RE = re.compile(r"^LLC ways:\s*(\d+)", re.M)
SIM_INS_RE = re.compile(r"^Simulation Instructions:\s*(\d+)", re.M)
WARM_INS_RE = re.compile(r"^Warmup Instructions:\s*(\d+)", re.M)

# Fills / borders (same palette as the reference workbook)
F_TITLE = PatternFill("solid", fgColor="1F4E78")
F_SUB = PatternFill("solid", fgColor="5B9BD5")
F_LIGHT = PatternFill("solid", fgColor="D9EAF7")
F_LIGHT2 = PatternFill("solid", fgColor="EEF5FB")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def fnt(bold=False, size=11, color="000000", italic=False):
    return Font(name=FONT, size=size, bold=bold, color=color, italic=italic)


def nat_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def benchmark_of(trace):
    return trace.rsplit("-", 1)[0] if "-" in trace else trace


# --------------------------------------------------------------------------
# 1) Parse logs
# --------------------------------------------------------------------------
def parse_log(path):
    txt = path.read_text(errors="replace")
    m_ipc, m_tot, m_ld = IPC_RE.search(txt), LLC_TOTAL_RE.search(txt), LLC_LOAD_RE.search(txt)
    if not (m_ipc and m_tot and m_ld):
        return None  # run unfinished / killed before "Region of Interest Statistics"
    ways = LLC_WAYS_RE.search(txt)
    sim = SIM_INS_RE.search(txt)
    warm = WARM_INS_RE.search(txt)
    return dict(
        ipc=float(m_ipc["ipc"]), ins=int(m_ipc["ins"]), cyc=int(m_ipc["cyc"]),
        tot_acc=int(m_tot["acc"]), tot_miss=int(m_tot["miss"]),
        ld_acc=int(m_ld["acc"]), ld_miss=int(m_ld["miss"]),
        log_ways=int(ways.group(1)) if ways else None,
        sim_ins=int(sim.group(1)) if sim else None,
        warm_ins=int(warm.group(1)) if warm else None,
    )


def collect(logs_dir):
    recs, skipped, mismatch, newest = {}, [], [], {}
    files = sorted(p for p in logs_dir.glob("*.txt") if p.is_file())
    for p in files:
        m = FILE_RE.match(p.name)
        if not m:
            continue
        trace, way, deg = m["trace"], int(m["way"]), int(m["deg"])
        r = parse_log(p)
        if r is None:
            skipped.append(p.name)
            continue
        if r["log_ways"] is not None and r["log_ways"] != way:
            mismatch.append((p.name, way, r["log_ways"]))
        key = (trace, way, deg)
        mt = p.stat().st_mtime
        if key in recs and newest[key] >= mt:  # duplicate file: keep the newest
            continue
        r["file"] = p.name
        recs[key] = r
        newest[key] = mt
    return recs, skipped, mismatch, len(files)


# --------------------------------------------------------------------------
# Helpers for Excel formulas / charts
# --------------------------------------------------------------------------
def col_ranges(cols, row, sheet="'IPC Surface'!"):
    """[3,4,5,9] -> "'IPC Surface'!C2:E2,'IPC Surface'!I2:I2" style comma list."""
    cols = sorted(cols)
    runs, start = [], None
    for i, c in enumerate(cols):
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            runs.append((start, prev))
            start = prev = c
    if start is not None:
        runs.append((start, prev))
    parts = []
    for a, b in runs:
        parts.append(f"{get_column_letter(a)}{row}:{get_column_letter(b)}{row}")
    return ",".join(parts) if len(parts) > 1 else parts[0]


def rich(sz, bold=False, color="404040"):
    cp = CharacterProperties(sz=sz, b=bold, solidFill=color)
    return RichText(bodyPr=RichTextProperties(),
                    p=[Paragraph(pPr=ParagraphProperties(defRPr=cp), endParaRPr=cp)])


def make_title(text, sz=1000, bold=True, color="1F4E78"):
    cp = CharacterProperties(sz=sz, b=bold, solidFill=color)
    rt = RichText(bodyPr=RichTextProperties(),
                  p=[Paragraph(pPr=ParagraphProperties(defRPr=cp),
                               r=[RegularTextRun(rPr=cp, t=text)])])
    return Title(tx=Text(rich=rt), overlay=False)


NICE = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8]


def nice_step(raw):
    """smallest 'nice' number (1,1.2,1.5,2,2.5,3,4,5,6,8 x 10^k) >= raw"""
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for n in NICE + [10]:
        if n * mag >= raw * 0.999999:
            return n * mag
    return 10 * mag


def ipc_axis(vals, data_frac=0.52, top_margin=0.04):
    """IPC axis (min, max, step) that keeps all IPC points in the TOP ~half of the plot,
    leaving the bottom band free for the LLC-miss bars."""
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 0.05 * hi, 0.01)
    rng = span / data_frac
    step = nice_step(rng / 7)
    amax = math.ceil((hi + top_margin * rng) / step - 1e-9) * step
    n = math.ceil((amax - lo) / data_frac / step - 1e-9)      # lo..amax uses <= data_frac of the axis
    amin = amax - n * step
    if amin < 0:                                              # never show a negative IPC axis
        amin = 0.0
    return round(amin, 6), round(amax, 6), round(step, 6)


def miss_axis(max_miss, bar_frac=0.30):
    """Miss axis (max, step) so the tallest bar fills ~30% of the plot height."""
    step = nice_step(max(max_miss, 1e-9) / bar_frac / 5)
    return round(5 * step, 6), round(step, 6)


DEG_COLORS = {0: "7F7F7F", 1: "5B9BD5", 2: "ED7D31", 4: "70AD47", 6: "7030A0", 8: "C00000"}
EXTRA_COLORS = ["00B0F0", "FFC000", "264478", "9E480E", "636363"]


def deg_color(d, i):
    return DEG_COLORS.get(d, EXTRA_COLORS[i % len(EXTRA_COLORS)])


# --------------------------------------------------------------------------
# Main workbook builder
# --------------------------------------------------------------------------
def build(recs, args):
    traces = sorted({k[0] for k in recs}, key=nat_key)
    WAYS = sorted(set(DEFAULT_WAYS) | {k[1] for k in recs})
    DEGS = sorted(set(DEFAULT_DEGREES) | {k[2] for k in recs})
    NGRID = len(WAYS) * len(DEGS)
    any_rec = next(iter(recs.values()))
    warm_m = (any_rec["warm_ins"] or 0) / 1e6
    sim_m = (any_rec["sim_ins"] or 0) / 1e6

    wb = Workbook()
    wb.calculation = CalcProperties(fullCalcOnLoad=True)   # Excel computes all formulas when the file is opened
    ws_surf = wb.active
    ws_surf.title = "IPC Surface"
    ws_tack = wb.create_sheet("Tacker Segregation")
    ws_chart = wb.create_sheet("IPC vs LLC Misses")
    ws_raw = wb.create_sheet("Raw Data")

    # ------------------------------------------------------------------ Raw Data
    raw_hdr = ["Trace", "Benchmark", "Way", "Prefetch Degree", "Degree", "IPC", "Instructions",
               "Cycles", "LLC Total Accesses", "LLC Total Misses", "LLC Load Accesses",
               "LLC Load Misses", "LLC Total Misses (M)", "LLC Load Misses (M)",
               "LLC Total MPKI", "LLC Load MPKI", "Log file"]
    for c, h in enumerate(raw_hdr, 1):
        cell = ws_raw.cell(1, c, h)
        cell.font, cell.fill, cell.border = fnt(True, color="FFFFFF"), F_TITLE, BOX
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws_raw.row_dimensions[1].height = 32
    raw_row = {}
    r = 2
    for t in traces:
        for d in DEGS:
            for w in WAYS:
                rec = recs.get((t, w, d))
                if not rec:
                    continue
                raw_row[(t, w, d)] = r
                vals = [t, benchmark_of(t), w, f"D{d}", d, rec["ipc"], rec["ins"], rec["cyc"],
                        rec["tot_acc"], rec["tot_miss"], rec["ld_acc"], rec["ld_miss"]]
                for c, v in enumerate(vals, 1):
                    cell = ws_raw.cell(r, c, v)
                    cell.font = fnt(color="0000FF" if c >= 6 else "000000")  # blue = parsed from log
                ws_raw.cell(r, 13, f"=J{r}/1000000")
                ws_raw.cell(r, 14, f"=L{r}/1000000")
                ws_raw.cell(r, 15, f"=IFERROR(J{r}/G{r}*1000,0)")
                ws_raw.cell(r, 16, f"=IFERROR(L{r}/G{r}*1000,0)")
                ws_raw.cell(r, 17, rec["file"]).font = fnt(color="0000FF")
                for c in (13, 14, 15, 16):
                    ws_raw.cell(r, c).font = fnt()
                ws_raw.cell(r, 6).number_format = "0.000000"
                for c in (7, 8, 9, 10, 11, 12):
                    ws_raw.cell(r, c).number_format = "#,##0"
                for c in (13, 14):
                    ws_raw.cell(r, c).number_format = "0.000000"
                for c in (15, 16):
                    ws_raw.cell(r, c).number_format = "0.000"
                r += 1
    raw_last = r - 1
    for c, wdt in enumerate([24, 16, 7, 11, 8, 12, 15, 15, 16, 15, 16, 15, 14, 14, 12, 12, 46], 1):
        ws_raw.column_dimensions[get_column_letter(c)].width = wdt
    ws_raw.freeze_panes = "C2"
    ws_raw.auto_filter.ref = f"A1:Q{raw_last}"
    note_r = raw_last + 2
    ws_raw.cell(note_r, 1, "Blue = value parsed from the ChampSim log (the only hard-coded numbers in this "
                           "workbook). Black = formula. (M) = millions. MPKI = misses per 1000 instructions.").font = \
        fnt(italic=True, size=9, color="595959")

    # ---------------------------------------------------------------- IPC Surface
    heads = ["Trace", "Benchmark"] + [f"W{w}_D{d}" for w in WAYS for d in DEGS]
    for c, h in enumerate(heads, 1):
        cell = ws_surf.cell(1, c, h)
        cell.font, cell.fill = fnt(True, color="FFFFFF"), F_TITLE
        cell.alignment = Alignment(horizontal="center")
    col_of = {}
    for i, w in enumerate(WAYS):
        for j, d in enumerate(DEGS):
            col_of[(w, d)] = 3 + i * len(DEGS) + j
    for ti, t in enumerate(traces):
        rr = 2 + ti
        ws_surf.cell(rr, 1, t).font = fnt()
        ws_surf.cell(rr, 2, benchmark_of(t)).font = fnt()
        for (w, d), c in col_of.items():
            if (t, w, d) in raw_row:
                cell = ws_surf.cell(rr, c, f"='Raw Data'!$F${raw_row[(t, w, d)]}")
                cell.font, cell.number_format = fnt(), "0.000000"
    ws_surf.column_dimensions["A"].width = 24
    ws_surf.column_dimensions["B"].width = 16
    for c in range(3, 3 + NGRID):
        ws_surf.column_dimensions[get_column_letter(c)].width = 11
    ws_surf.freeze_panes = "C2"
    surf_last_col = get_column_letter(2 + NGRID)

    # ------------------------------------------------------- Tacker Segregation
    low_ways = [w for w in WAYS if w in args.low_ways]
    high_ways = [w for w in WAYS if w in args.high_ways]
    low_degs = [d for d in DEGS if d in args.low_degrees]
    high_degs = [d for d in DEGS if d in args.high_degrees]
    t = ws_tack
    t.merge_cells("A1:J1")
    t["A1"] = "Workload / Tacker Segregation — LLC Capacity × Prefetch Benefit"
    t["A1"].font, t["A1"].fill = fnt(True, 14, "FFFFFF"), F_TITLE
    t["A1"].alignment = Alignment(vertical="center")
    t.row_dimensions[1].height = 24
    t.merge_cells("A2:J3")
    t["A2"] = (f"Method: Cache benefit = relative IPC gain of high-cache ({'/'.join(map(str, high_ways))} ways) over "
               f"low-cache ({'/'.join(map(str, low_ways))} ways), averaged across all prefetch degrees. "
               f"Prefetch benefit = relative IPC gain of high-prefetch ({'/'.join('D%d' % d for d in high_degs)}) over "
               f"low-prefetch ({'/'.join('D%d' % d for d in low_degs)}), averaged across all LLC ways. "
               f"High/low is split at the median across traces with a complete sweep; equality is classified as High. "
               f"Traces with missing (way, degree) runs are shown as Incomplete and excluded from the medians.")
    t["A2"].font = fnt(size=10)
    t["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    t.row_dimensions[2].height = 30
    t.row_dimensions[3].height = 30

    first, last = 9, 8 + len(traces)
    for c, h in ((1, "Threshold"), (2, "Value")):
        cell = t.cell(4, c, h)
        cell.font, cell.fill = fnt(True), F_LIGHT
    t["A5"], t["A6"], t["A7"] = "Median cache benefit", "Median prefetch benefit", "Runs expected per trace"
    t["B5"] = f'=IFERROR(MEDIAN(E{first}:E{last}),"")'
    t["B6"] = f'=IFERROR(MEDIAN(H{first}:H{last}),"")'
    t["B7"] = f"=COUNTA('IPC Surface'!C1:{surf_last_col}1)"
    for a in ("A5", "A6", "A7", "B5", "B6", "B7"):
        t[a].font = fnt()
    t["B5"].number_format = t["B6"].number_format = "0.00%"
    t["B7"].number_format = "0"

    th = ["Trace", "Benchmark", "Low-cache Avg IPC", "High-cache Avg IPC", "Cache Benefit %",
          "Low-prefetch Avg IPC", "High-prefetch Avg IPC", "Prefetch Benefit %", "Classification",
          "Runs found"]
    for c, h in enumerate(th, 1):
        cell = t.cell(8, c, h)
        cell.font, cell.fill = fnt(True, color="FFFFFF"), F_SUB
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    low_cache_cols = [col_of[(w, d)] for w in low_ways for d in DEGS]
    high_cache_cols = [col_of[(w, d)] for w in high_ways for d in DEGS]
    low_pf_cols = [col_of[(w, d)] for w in WAYS for d in low_degs]
    high_pf_cols = [col_of[(w, d)] for w in WAYS for d in high_degs]

    def avg_formula(cols, sr):
        parts = ",".join(f"'IPC Surface'!{p}" for p in col_ranges(cols, sr).split(","))
        return f'=IFERROR(AVERAGE({parts}),"")'

    for ti, tr in enumerate(traces):
        rr, sr = first + ti, 2 + ti
        t.cell(rr, 1, tr)
        t.cell(rr, 2, benchmark_of(tr))
        t.cell(rr, 3, avg_formula(low_cache_cols, sr))
        t.cell(rr, 4, avg_formula(high_cache_cols, sr))
        t.cell(rr, 5, f'=IF(OR($J{rr}<$B$7,C{rr}="",D{rr}=""),"",IFERROR((D{rr}-C{rr})/C{rr},0))')
        t.cell(rr, 6, avg_formula(low_pf_cols, sr))
        t.cell(rr, 7, avg_formula(high_pf_cols, sr))
        t.cell(rr, 8, f'=IF(OR($J{rr}<$B$7,F{rr}="",G{rr}=""),"",IFERROR((G{rr}-F{rr})/F{rr},0))')
        t.cell(rr, 9, (f'=IF($J{rr}<$B$7,"Incomplete ("&$J{rr}&"/"&$B$7&" runs)",'
                       f'IF(AND(E{rr}>=$B$5,H{rr}>=$B$6),"High-cache / High-prefetch",'
                       f'IF(AND(E{rr}>=$B$5,H{rr}<$B$6),"High-cache / Low-prefetch",'
                       f'IF(AND(E{rr}<$B$5,H{rr}>=$B$6),"Low-cache / High-prefetch",'
                       f'"Low-cache / Low-prefetch"))))'))
        t.cell(rr, 10, f"=COUNT('IPC Surface'!C{sr}:{surf_last_col}{sr})")
        for c in range(1, 11):
            t.cell(rr, c).font = fnt()
        for c in (3, 4, 6, 7):
            t.cell(rr, c).number_format = "0.000000"
        for c in (5, 8):
            t.cell(rr, c).number_format = "0.00%"
        t.cell(rr, 10).number_format = "0"
    for c, wdt in enumerate([24, 16, 16, 16, 15, 17, 18, 17, 30, 11], 1):
        t.column_dimensions[get_column_letter(c)].width = wdt

    # Python-side snapshot of the classification (for the static group lists)
    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    stats = {}
    for tr in traces:
        ipc = lambda w, d: recs[(tr, w, d)]["ipc"] if (tr, w, d) in recs else None
        n = sum(1 for w in WAYS for d in DEGS if (tr, w, d) in recs)
        lc = avg([ipc(w, d) for w in low_ways for d in DEGS])
        hc = avg([ipc(w, d) for w in high_ways for d in DEGS])
        lp = avg([ipc(w, d) for w in WAYS for d in low_degs])
        hp = avg([ipc(w, d) for w in WAYS for d in high_degs])
        stats[tr] = dict(n=n, cb=(hc - lc) / lc if n == NGRID and lc and hc else None,
                         pb=(hp - lp) / lp if n == NGRID and lp and hp else None)
    complete = [tr for tr in traces if stats[tr]["cb"] is not None and stats[tr]["pb"] is not None]

    def median(v):
        s = sorted(v)
        n = len(s)
        return None if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)

    med_c = median([stats[x]["cb"] for x in complete])
    med_p = median([stats[x]["pb"] for x in complete])
    groups = {k: [] for k in ("High-cache / High-prefetch", "High-cache / Low-prefetch",
                              "Low-cache / High-prefetch", "Low-cache / Low-prefetch", "Incomplete")}
    for tr in traces:
        if tr not in complete:
            groups["Incomplete"].append(tr)
        else:
            hi_c, hi_p = stats[tr]["cb"] >= med_c, stats[tr]["pb"] >= med_p
            groups[f"{'High' if hi_c else 'Low'}-cache / {'High' if hi_p else 'Low'}-prefetch"].append(tr)

    gr = last + 3
    t.merge_cells(start_row=gr, start_column=1, end_row=gr, end_column=9)
    t.cell(gr, 1, "Segregated Workload Groups").font = fnt(True, 12, "FFFFFF")
    t.cell(gr, 1).fill = F_TITLE
    gr += 1
    t.merge_cells(start_row=gr, start_column=1, end_row=gr, end_column=9)
    t.cell(gr, 1, "Membership lists are a snapshot written when this workbook was generated; the Classification "
                  "column above recalculates live (re-run the script after new runs finish). "
                  "Benefit values below are live lookups.").font = fnt(italic=True, size=9, color="595959")
    gr += 2
    rng = lambda col: f"${col}${first}:${col}${last}"
    for name, members in groups.items():
        t.merge_cells(start_row=gr, start_column=1, end_row=gr, end_column=4)
        if name == "Incomplete":
            if not members:
                continue
            t.cell(gr, 1, f'="Incomplete sweep — not classified ("&COUNTIF({rng("I")},"Incomplete*")&" traces)"')
        else:
            t.cell(gr, 1, f'="{name} ("&COUNTIF({rng("I")},"{name}")&" traces)"')
        t.cell(gr, 1).font, t.cell(gr, 1).fill = fnt(True), F_LIGHT
        gr += 1
        for c, h in enumerate(["Trace", "Benchmark", "Cache Benefit %", "Prefetch Benefit %"], 1):
            cell = t.cell(gr, c, h)
            cell.font, cell.fill = fnt(True, color="FFFFFF"), F_SUB
        gr += 1
        if not members:
            t.cell(gr, 1, "(none)").font = fnt(italic=True, color="7F7F7F")
            gr += 3
            continue
        for tr in members:
            t.cell(gr, 1, tr).font = fnt()
            t.cell(gr, 2, f"=INDEX({rng('B')},MATCH($A{gr},{rng('A')},0))").font = fnt()
            for c, src in ((3, "E"), (4, "H")):
                cell = t.cell(gr, c, f'=INDEX({rng(src)},MATCH($A{gr},{rng("A")},0))')
                cell.font, cell.number_format = fnt(), "0.00%"
            gr += 1
        gr += 2

    # ------------------------------------------------------ IPC vs LLC Misses
    ch = ws_chart
    metric = args.miss_metric
    metric_txt = {"both": "LLC misses (total and load)", "total": "LLC total misses",
                  "load": "LLC load misses"}[metric]
    ch.merge_cells("A1:R1")
    ch["A1"] = f"IPC vs LLC Misses — {args.title}"
    ch["A1"].font, ch["A1"].fill = fnt(True, 16, "FFFFFF"), F_TITLE
    ch["A1"].alignment = Alignment(vertical="center")
    ch.row_dimensions[1].height = 28
    ch.merge_cells("A2:R2")
    ch["A2"] = (f"One chart per trace. X axis (left → right): LLC ways 1 → 2 → 4 → 8 → 16 → 32. "
                f"Bars (right axis): {metric_txt} in millions, from the D{args.miss_degree} (no-prefetch) run, so they show "
                f"the effect of capacity alone. Lines (left axis, bottom → top): IPC, one line per prefetch degree "
                f"D{'/D'.join(map(str, DEGS))}. Warmup {warm_m:g}M / simulation {sim_m:g}M instructions.")
    ch["A2"].font, ch["A2"].fill = fnt(size=10), F_LIGHT
    ch["A2"].alignment = Alignment(wrap_text=True, vertical="center")
    ch.row_dimensions[2].height = 44
    ch.merge_cells("A3:R3")
    ch["A3"] = ("Reading a chart: bars shrinking to the right = more cache removes misses; lines rising = higher IPC. "
                "The IPC axis is zoomed per trace (does not start at 0) so small effects stay visible. Gaps = runs "
                "not finished yet. The data behind every chart is below the charts; its source is the 'Raw Data' tab.")
    ch["A3"].font, ch["A3"].fill = fnt(size=9), F_LIGHT2
    ch["A3"].alignment = Alignment(wrap_text=True, vertical="center")
    ch.row_dimensions[3].height = 32

    ch.column_dimensions["A"].width = 26
    for c in range(2, 19):
        ch.column_dimensions[get_column_letter(c)].width = 11

    CH_ROWS, CH_W_CM, CH_H_CM = 21, 17.5, 10.0
    n_chart_rows = math.ceil(len(traces) / 2)
    feed_top = 5 + n_chart_rows * CH_ROWS + 1
    ch.cell(feed_top - 1, 1, "Chart data (feeds the charts above; every cell links to the 'Raw Data' tab)").font = \
        fnt(True, 12, "1F4E78")

    miss_cols = {"both": [("Total LLC misses (M)", "M"), ("Load LLC misses (M)", "N")],
                 "total": [("Total LLC misses (M)", "M")],
                 "load": [("Load LLC misses (M)", "N")]}[metric]
    bar_colors = {"Total LLC misses (M)": "BDD7EE", "Load LLC misses (M)": "8FAADC"}
    bar_edge = {"Total LLC misses (M)": "9DC3E6", "Load LLC misses (M)": "2F5597"}
    blk = len(WAYS) + 3
    for k, tr in enumerate(traces):
        base_deg = args.miss_degree if any((tr, w, args.miss_degree) in recs for w in WAYS) else \
            min(d for d in DEGS if any((tr, w, d) in recs for w in WAYS))
        top = feed_top + k * blk
        ch.cell(top, 1, tr).font = fnt(True, color="1F4E78")
        hdr = top + 1
        hdrs = ["LLC ways (X axis)"] + [m[0] for m in miss_cols] + \
               [f"IPC D{d}" + (" (no prefetch)" if d == 0 else "") for d in DEGS]
        for c, h in enumerate(hdrs, 1):
            cell = ch.cell(hdr, c, h)
            cell.font, cell.fill, cell.border = fnt(True, 10, "FFFFFF"), F_SUB, BOX
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
        ch.row_dimensions[hdr].height = 30
        ipc_vals = []
        for wi, w in enumerate(WAYS):
            rr = hdr + 1 + wi
            ch.cell(rr, 1, f"{w} way" if w == 1 else f"{w} ways").font = fnt()
            ch.cell(rr, 1).alignment = Alignment(horizontal="center")
            for mi, (_, rawcol) in enumerate(miss_cols):
                if (tr, w, base_deg) in raw_row:
                    cell = ch.cell(rr, 2 + mi, f"='Raw Data'!{rawcol}{raw_row[(tr, w, base_deg)]}")
                    cell.font, cell.number_format = fnt(), "0.00"
            for di, d in enumerate(DEGS):
                if (tr, w, d) in raw_row:
                    cell = ch.cell(rr, 2 + len(miss_cols) + di, f"='Raw Data'!F{raw_row[(tr, w, d)]}")
                    cell.font, cell.number_format = fnt(), "0.000000"
                    ipc_vals.append(recs[(tr, w, d)]["ipc"])
        last_r = hdr + len(WAYS)

        cats = Reference(ch, min_col=1, min_row=hdr + 1, max_row=last_r)
        # -- bars: LLC misses (right axis)
        bar = BarChart()
        bar.type, bar.grouping = "col", "clustered"
        bar.add_data(Reference(ch, min_col=2, max_col=1 + len(miss_cols), min_row=hdr, max_row=last_r),
                     titles_from_data=True)
        bar.set_categories(cats)
        for s, (nm, _) in zip(bar.series, miss_cols):
            s.graphicalProperties.solidFill = bar_colors[nm]
            s.graphicalProperties.line.solidFill = bar_edge[nm]
        bar.gapWidth = 40
        bar.overlap = 0
        bar.dataLabels = DataLabelList()
        bar.dataLabels.showVal = True
        bar.dataLabels.showSerName = bar.dataLabels.showCatName = bar.dataLabels.showLegendKey = False
        bar.dataLabels.position = "outEnd"
        bar.dataLabels.txPr = rich(700, color="595959")
        bar.y_axis.majorGridlines = None
        bar.y_axis.crosses = "max"            # misses axis on the right
        bar.y_axis.scaling.min = 0
        miss_vals = [recs[(tr, w, base_deg)][key] / 1e6 for w in WAYS if (tr, w, base_deg) in recs
                     for key in {"both": ("tot_miss", "ld_miss"), "total": ("tot_miss",), "load": ("ld_miss",)}[metric]]
        mx, mstep = miss_axis(max(miss_vals) if miss_vals else 1.0)
        bar.y_axis.scaling.max, bar.y_axis.majorUnit = mx, mstep
        bar.y_axis.number_format = "0.0" if mx >= 2 else "0.00"
        bar.y_axis.txPr = rich(800, False, "5B7DB1")
        bar.x_axis.txPr = rich(900, True)
        bar.x_axis.delete = bar.y_axis.delete = False

        # -- lines: IPC (left axis)
        line = LineChart()
        line.add_data(Reference(ch, min_col=2 + len(miss_cols), max_col=1 + len(miss_cols) + len(DEGS),
                                min_row=hdr, max_row=last_r), titles_from_data=True)
        line.set_categories(cats)
        for i, (s, d) in enumerate(zip(line.series, DEGS)):
            colr = deg_color(d, i)
            s.graphicalProperties.line.solidFill = colr
            s.graphicalProperties.line.width = 22225
            if d == 0:
                s.graphicalProperties.line.dashStyle = "dash"
            s.marker.symbol, s.marker.size = "circle", 5
            s.marker.graphicalProperties = None
            s.marker.graphicalProperties = GraphicalProperties(solidFill=colr)
            s.marker.graphicalProperties.line.solidFill = colr
            s.smooth = False
        line.y_axis.axId = 200                # IPC axis on the left
        line.y_axis.number_format = "0.00"
        line.y_axis.txPr = rich(800, True, "1F4E78")
        line.y_axis.delete = False
        if ipc_vals:
            amin, amax, astep = ipc_axis(ipc_vals)
            line.y_axis.scaling.min, line.y_axis.scaling.max, line.y_axis.majorUnit = amin, amax, astep

        bar.title = make_title(f"{tr}   —   lines: IPC (left axis)   ·   bars: LLC misses, millions (right axis)", 1000)
        bar.legend.position = "b"
        bar.legend.txPr = rich(800)
        bar.display_blanks = "gap"
        bar += line
        bar.width, bar.height = CH_W_CM, CH_H_CM
        crow = 5 + (k // 2) * CH_ROWS
        ch.add_chart(bar, f"{'A' if k % 2 == 0 else 'I'}{crow}")

    # printing: landscape, one page wide
    from openpyxl.worksheet.properties import PageSetupProperties
    for w_ in (ws_chart, ws_tack, ws_surf):
        w_.page_setup.orientation = "landscape"
        w_.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        w_.page_setup.fitToWidth, w_.page_setup.fitToHeight = 1, 0

    return wb, dict(traces=traces, WAYS=WAYS, DEGS=DEGS, NGRID=NGRID, groups=groups, complete=complete,
                    stats=stats, medians=(med_c, med_p))


def main():
    here = Path(__file__).resolve().parent
    root = here.parent if here.name == "scripts" else here
    ap = argparse.ArgumentParser(description="Build the IPC-vs-LLC-misses workbook from ChampSim characterization logs.")
    ap.add_argument("--logs-dir", type=Path, default=root / "results_250M" / "characterization")
    ap.add_argument("--out", type=Path, default=Path("IPStride_SRRIP_IPC_vs_LLC_Misses.xlsx"))
    ap.add_argument("--title", default="L2C IP-Stride Prefetch + SRRIP")
    ap.add_argument("--miss-metric", choices=["both", "total", "load"], default="both",
                    help="LLC miss counter drawn as bars: 'LLC TOTAL' miss, 'LLC LOAD' miss, or both (default)")
    ap.add_argument("--miss-degree", type=int, default=0,
                    help="prefetch degree whose miss counts are drawn as bars (default 0 = no prefetch)")
    ap.add_argument("--low-ways", type=lambda s: [int(x) for x in s.split(",")], default=[1, 2, 4])
    ap.add_argument("--high-ways", type=lambda s: [int(x) for x in s.split(",")], default=[8, 16, 32])
    ap.add_argument("--low-degrees", type=lambda s: [int(x) for x in s.split(",")], default=[0, 1, 2])
    ap.add_argument("--high-degrees", type=lambda s: [int(x) for x in s.split(",")], default=[4, 6, 8])
    args = ap.parse_args()

    if not args.logs_dir.is_dir():
        sys.exit(f"[ERROR] logs directory not found: {args.logs_dir}")
    recs, skipped, mismatch, nfiles = collect(args.logs_dir)
    if not recs:
        sys.exit(f"[ERROR] no complete '<trace>_way<W>_deg<D>.txt' logs found in {args.logs_dir}")

    wb, info = build(recs, args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(args.out)

    print(f"Logs directory : {args.logs_dir}  ({nfiles} .txt files)")
    print(f"Parsed         : {len(recs)} complete runs, {len(info['traces'])} traces "
          f"(grid {len(info['WAYS'])} ways x {len(info['DEGS'])} degrees = {info['NGRID']} runs/trace)")
    full = len(info["complete"])
    print(f"Full sweeps    : {full} traces complete, {len(info['traces']) - full} incomplete")
    if skipped:
        print(f"[WARN] {len(skipped)} unfinished/unparseable logs skipped (no 'Region of Interest' stats), e.g. {skipped[:3]}")
    for name, w, lw in mismatch[:5]:
        print(f"[WARN] {name}: filename says way{w} but the log says 'LLC ways: {lw}'")
    for tr in info["traces"]:
        miss = [(w, d) for w in info["WAYS"] for d in info["DEGS"] if (tr, w, d) not in recs]
        if miss:
            print(f"  missing {len(miss):>2}/{info['NGRID']} for {tr}")
    print(f"Written        : {args.out.resolve()}")
    print("Tip: open in Excel/LibreOffice; if cells show no values, press Ctrl+Alt+F9 (full recalculation).")


if __name__ == "__main__":
    main()
