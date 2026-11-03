#!/bin/bash

Target directory

OUTPUT_DIR="dpc3_traces"

Base URL for Stony Brook DPC-3 traces

BASE_URL="https://dpc3.compas.cs.stonybrook.edu/champsim-traces/speccpu"

50 diverse traces covering pointer-chasing, regular streaming,

branch-heavy integer, and dense scientific floating-point workloads.

TRACES=(
# --- Pointer-Chasing & Irregular Memory Access ---
"429.mcf-184B.champsimtrace.xz"
"429.mcf-192B.champsimtrace.xz"
"473.astar-42B.champsimtrace.xz"
"473.astar-153B.champsimtrace.xz"
"473.astar-359B.champsimtrace.xz"
"483.xalancbmk-736B.champsimtrace.xz"
"605.mcf_s-1152B.champsimtrace.xz"
"605.mcf_s-1536B.champsimtrace.xz"
"620.omnetpp_s-141B.champsimtrace.xz"
"623.xalancbmk_s-10B.champsimtrace.xz"

# --- Regular Stride & High Memory Bandwidth (Streaming) ---
"410.bwaves-945B.champsimtrace.xz"
"410.bwaves-2097B.champsimtrace.xz"
"459.GemsFDTD-765B.champsimtrace.xz"
"459.GemsFDTD-1320B.champsimtrace.xz"
"459.GemsFDTD-1418B.champsimtrace.xz"
"470.lbm-1274B.champsimtrace.xz"
"603.bwaves_s-1740B.champsimtrace.xz"
"603.bwaves_s-2609B.champsimtrace.xz"
"619.lbm_s-2676B.champsimtrace.xz"
"649.fotonik3d_s-1176B.champsimtrace.xz"
"654.roms_s-293B.champsimtrace.xz"

# --- Branch-Heavy, Complex Control-Flow & Integer ---
"400.perlbench-41B.champsimtrace.xz"
"403.gcc-17B.champsimtrace.xz"
"456.hmmer-191B.champsimtrace.xz"
"464.h264ref-30B.champsimtrace.xz"
"464.h264ref-57B.champsimtrace.xz"
"464.h264ref-97B.champsimtrace.xz"
"600.perlbench_s-210B.champsimtrace.xz"
"600.perlbench_s-570B.champsimtrace.xz"
"602.gcc_s-734B.champsimtrace.xz"
"602.gcc_s-1850B.champsimtrace.xz"
"602.gcc_s-2226B.champsimtrace.xz"
"602.gcc_s-2375B.champsimtrace.xz"

# --- Dense Floating Point & Scientific Computing ---
"416.gamess-875B.champsimtrace.xz"
"454.calculix-104B.champsimtrace.xz"
"454.calculix-460B.champsimtrace.xz"
"462.libquantum-714B.champsimtrace.xz"
"465.tonto-44B.champsimtrace.xz"
"465.tonto-1914B.champsimtrace.xz"
"471.omnetpp-188B.champsimtrace.xz"
"481.wrf-196B.champsimtrace.xz"
"481.wrf-1170B.champsimtrace.xz"
"481.wrf-1254B.champsimtrace.xz"
"481.wrf-1281B.champsimtrace.xz"
"482.sphinx3-234B.champsimtrace.xz"
"482.sphinx3-1100B.champsimtrace.xz"
"607.cactuBSSN_s-2421B.champsimtrace.xz"
"621.wrf_s-6673B.champsimtrace.xz"
"627.cam4_s-490B.champsimtrace.xz"
"628.pop2_s-17B.champsimtrace.xz"


)

Ensure output directory exists

mkdir -p "$OUTPUT_DIR"

TOTAL=${#TRACES[@]}
echo ""
echo "[+] Target Folder : $OUTPUT_DIR/"
echo "[+] Total Traces  : $TOTAL"
echo ""

COUNT=1
for TRACE in "${TRACES[@]}"; do
TARGET_PATH="$OUTPUT_DIR/$TRACE"
URL="$BASE_URL/$TRACE"

if [ -f "$TARGET_PATH" ]; then
    echo "[$COUNT/$TOTAL] [SKIP]$TRACE already exists."
else
    echo "[$COUNT/$TOTAL] [DOWNLOADING]$TRACE..."
    wget --no-check-certificate -q --show-progress -O "$TARGET_PATH" "$URL" || \
    curl -sSL -k -o "$TARGET_PATH" "$URL"

    if [ $? -eq 0 ] && [ -s "$TARGET_PATH" ]; then
        echo "         └─ Successfully saved to $TARGET_PATH"
    else
        echo "         └─ [ERROR] Failed to download $TRACE"
        rm -f "$TARGET_PATH"
    fi
fi
((COUNT++))


done

echo ""
echo "[+] Download completed! All traces are saved in $OUTPUT_DIR/"
echo ""