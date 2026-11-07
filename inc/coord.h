// Coordinated prefetch-throttling / LLC-partitioning controller (common to every prefetcher).
//
// Policies (COORD_POLICY):
//   off     (default) the module does nothing; results are bit-identical to a build without it.
//   log     observation only: every COORD_EPOCH_CYCLES uncore cycles after warm-up it prints one COORD_G line
//           (global) and one COORD_C line per core with the deltas of the epoch that just ended.
//   static  like log, and additionally applies fixed actuator settings for the whole run:
//             COORD_STATIC_LEVEL  prefetch level, one value for all cores ("2") or one per core ("3-1-0-2").
//                                 0 = prefetching off, 1 = 1/4 of requests, 2 = 1/2, 3 = all (native, default).
//             COORD_STATIC_WAYS   LLC way allocation, "equal" or one value per core ("2-2-6-6"; sum = LLC_WAY,
//                                 each >= 1). Needs the ucp LLC replacement. UCP's own repartition timer is
//                                 switched off. If unset, UCP keeps allocating by itself.
//
// Environment:
//   COORD_EPOCH_CYCLES  epoch length in uncore cycles (default 1000000)
//
// The throttle sits in CACHE::prefetch_line()/kpc_prefetch_line(), the common entry point of every prefetcher
// into the private L1D and L2C prefetch queues, so it works for any prefetcher without editing it.
// Note: the prefetcher still believes a throttled request was issued, so prefetchers that learn from their own
// issued requests (e.g. Pythia) see slightly different feedback than with a native degree cap.
#ifndef COORD_H
#define COORD_H

#include <cstdint>

#define COORD_LEVELS 4          // 0 = off, 1 = 1/4, 2 = 1/2, 3 = all requests allowed

extern bool coord_armed;        // true between end of warm-up and end of simulation, if policy != off
extern bool coord_gate_active;  // true only if some core is throttled (level < 3); keeps the hot path free otherwise

void coord_init();              // read environment, call once before the main loop
void coord_arm();               // call once when warm-up finishes (after cache stats were reset)
void coord_tick();              // call every uncore cycle after uncore.cycle++ (guard with coord_armed)

// Called from CACHE::prefetch_line() when coord_gate_active. Returns false if this request must be dropped.
bool coord_pf_allow(uint32_t cpu, uint8_t cache_type);
int  coord_level(uint32_t cpu); // current prefetch level of a core (for prefetcher adapters)

#endif /* COORD_H */
