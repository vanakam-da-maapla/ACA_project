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
//   victim  victim-aware, partition-aware prefetch throttling (VPT).  UCP keeps partitioning the LLC; every epoch
//           the controller looks at DRAM pressure and at each core's prefetch DRAM traffic.  Under pressure it
//           TRIES throttling, in turn, the core with the most USELESS prefetch traffic (self-harm, e.g. inaccurate
//           IPCP on irregular code) and the core with the most prefetch traffic of any kind (bandwidth hog that
//           hurts its neighbours, e.g. an accurate streamer next to cache-sensitive cores) (one level
//           down for COORD_V_TRIAL epochs) and KEEPS the change only if the harmonic mean of every core's IPC
//           relative to its recent IPC improves by COORD_V_GAIN -- i.e. only if the other cores (the victims the
//           partition is protecting) gain more than the throttled core loses.  When DRAM is calm, or every
//           COORD_V_RETEST epochs, throttled cores are tried one level up again (phase changes).  Rejected trials
//           put the core in a cool-down.  Partition awareness: the score is measured on the cores' IPC under the
//           current UCP allocation, so the controller only keeps throttling that helps the cores UCP protects, and
//           UCP's utility monitor sees the reduced prefetch fills at its next repartition.
//           COORD_V_BW      DRAM pressure threshold: mean DRAM bandwidth level over the epoch (0..3, Pythia's
//                           quartiles of peak bandwidth), default 1.5 (= busy about half the time or more)
//           COORD_V_GAIN    minimum relative gain to keep a throttle step (default 0.005)
//           COORD_V_TRIAL   epochs per trial (default 2);  COORD_V_COOLDOWN  epochs after a rejected trial (16)
//           COORD_V_CALM    calm epochs before trying a core one level up (4);  COORD_V_RETEST  forced retest (32)
//           COORD_V_SHARE   a candidate's useless-prefetch traffic must be >= this x the per-core average (0.5)
//           COORD_V_PART=1  partition inputs from UCP (its current ways per core): (1) the trial score weights every
//                           core by 1 + ways/average ways, so cores UCP protects as cache-sensitive count more;
//                           (2) the bandwidth-hog ranking scales prefetch traffic by average ways / ways, so cores
//                           UCP gives few ways (little cache benefit, typically streamers) are suspected first
//           COORD_V_BWLO    DRAM counts as calm only below this level (hysteresis; default: below COORD_V_BW)
//           COORD_V_UPGAIN  an up-trial is kept if the score >= 1 + this (default -COORD_V_GAIN/2)
//           COORD_V_SCORE   hm (default: harmonic mean, fairness-aware) or am (arithmetic mean, throughput)
//   Two-way coordination (the controller also owns the LLC partition; needs the ucp LLC replacement):
//           COORD_V_WAYS    0 (default) UCP partitions by itself.  1 or 2: UCP's own repartition timer is switched off
//                           and every COORD_V_WPERIOD epochs (default 5 = UCP's 5M-cycle interval) the controller
//                           computes the allocation itself with UCP's lookahead algorithm on UCP's shadow tags, whose
//                           hits are now counted separately for demand and prefetch requests:
//                             1 = prefetch-aware utility: demand hits + (prefetch accuracy of the core) x prefetch hits,
//                                 so an inaccurate prefetcher cannot inflate its core's utility and take ways from
//                                 cache-sensitive neighbours, while accurate prefetchers keep the ways they earn
//                             2 = UCP's own utility (demand + prefetch hits; ablation of the accuracy weighting)
//                           A changed allocation is a TRIAL like a throttle step (COORD_V_WTRIAL epochs, default 2),
//                           one change at a time: kept unless the score drops below 1 - COORD_V_WTOL (default
//                           COORD_V_GAIN), otherwise the previous allocation is restored and the rejected one is not
//                           retried for COORD_V_WCOOL periods (default 3).
//           COORD_V_SNAP=1  rollback chain (needs COORD_V_WAYS): when a reverted allocation had given ways to a core
//                           whose prefetch accuracy is below COORD_V_ACCLO (default 0.4), the controller next tries
//                           throttling that core (its prefetches inflated its utility).  If that is rejected as well,
//                           the last-good joint setting (all prefetch levels + way allocation, saved after every kept
//                           decision) is restored.
//
// Environment:
//   COORD_EPOCH_CYCLES  epoch length in uncore cycles (default 1000000)
//   COORD_QUIET=1       do not print the per-epoch COORD_G / COORD_C lines (COORD_V decision lines are kept)
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
void coord_final_stats();       // end-of-run summary (victim policy)

#endif /* COORD_H */
