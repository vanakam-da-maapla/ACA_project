#!/usr/bin/env python3
"""Convert COORD_C / COORD_G lines of a ChampSim log into CSV.

usage: epoch_log_to_csv.py <champsim_log> [-o out.csv]
One row per (epoch, core); the global DRAM columns of the same epoch are repeated on every row.
"""
import argparse, csv, re, sys

KV = re.compile(r'(\w+)=([^\s]+)')

def parse(path):
    glob, rows = {}, []
    with open(path, errors='replace') as f:
        for line in f:
            if line.startswith('COORD_G '):
                d = dict(KV.findall(line)); glob[d['e']] = d
            elif line.startswith('COORD_C '):
                rows.append(dict(KV.findall(line)))
    out = []
    for r in rows:
        g = glob.get(r['e'], {})
        r['cycle'] = g.get('cycle', '')
        r['dram_bw'] = g.get('dram_bw', '')
        r['dram_enq'] = g.get('dram_enq', '')
        out.append(r)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('log'); ap.add_argument('-o', '--out', default='-')
    a = ap.parse_args()
    rows = parse(a.log)
    if not rows:
        sys.exit('no COORD_C lines found (run with COORD_POLICY=log)')
    cols = list(rows[0].keys())
    fh = sys.stdout if a.out == '-' else open(a.out, 'w', newline='')
    w = csv.DictWriter(fh, fieldnames=cols); w.writeheader(); w.writerows(rows)

if __name__ == '__main__':
    main()
