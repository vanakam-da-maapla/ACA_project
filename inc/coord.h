// Coordinated prefetch-throttling / LLC-partitioning controller (common to every prefetcher).
//
// Step 1 (this file set): observation only. Every COORD_EPOCH_CYCLES uncore cycles after warm-up the module
// snapshots per-core and global counters, computes the deltas of the last epoch and prints one "COORD" line per
// core plus one global line. It never changes simulator state, so results are bit-identical to a build without it.
//
// Environment:
//   COORD_POLICY        off (default) | log
//   COORD_EPOCH_CYCLES  epoch length in uncore cycles (default 1000000)
#ifndef COORD_H
#define COORD_H

#include <cstdint>

extern bool coord_armed;     // true only between end of warm-up and end of simulation, and only if the policy != off

void coord_init();           // read environment, call once before the main loop
void coord_arm();            // call once when warm-up finishes (after cache stats were reset)
void coord_tick();           // call every uncore cycle after uncore.cycle++ (guard with coord_armed)

#endif /* COORD_H */
