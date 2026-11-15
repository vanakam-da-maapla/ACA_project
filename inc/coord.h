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
//   v2.1:
//           COORD_V_FIX=1   (a) a snapshot restore puts only the offending core in cool-down (v2 froze every core's
//                           throttling for COORD_V_COOLDOWN epochs after each restore); (b) after a reverted way trial,
//                           no way trial for COORD_V_WCOOL periods (v2 only skipped the identical allocation)
//           COORD_V_WMIN    trial a new allocation only if it moves at least this many ways (default 1)
//           COORD_V_WAYS=3  PSR-guided partition.  defaultAlloc = lookahead on demand hits only, prefetchAlloc = on
//                           demand + prefetch hits.  Start from defaultAlloc; a core that prefetchAlloc gives more ways
//                           gets COORD_V_GRANTHI (0.8) of the extra ways if its prefetch success rate PSR = accuracy x
//                           timeliness >= COORD_V_PSRHI (0.6), COORD_V_GRANTMID (0.4) if >= COORD_V_PSRLO (0.3), none
//                           below (and it is then throttled as a polluter, keep-or-revert).  Ways come from donors
//                           (cores prefetchAlloc does not enlarge), at most COORD_V_DONOR (1/3) of each donor's ways,
//                           >= 1 way kept, always from the donor that loses the fewest demand hits.
//   v2.2 (all off by default):
//           COORD_V_WAYS=4  reclaim partition: start from UCP's own split (prefetchAlloc); a core that it gives more ways
//                           than the demand-only split (defaultAlloc) keeps PSR^COORD_V_GAMMA of those prefetch-won ways;
//                           the reclaimed ways go one by one to the core whose next way has the most demand hits.  With
//                           accurate, timely prefetchers the split equals UCP's.  Acts only if prefetch-won ways >=
//                           COORD_V_WTRIG.  A requester with PSR < COORD_V_PSRLO is throttled in the same trial
//                           (coupled move: the trial must then improve the score by COORD_V_GAIN, like a throttle step).
//           COORD_V_WSTEP   move at most this many ways per period
//           COORD_V_GUARD   victim guard: reject a down / way trial if any other core's IPC drops by more than this
//           COORD_V_DROP2   a pressure throttle drops two levels at once if the core's accuracy is below this
//           COORD_V_POLL=1  rank self-harm suspects also by the share of their LLC blocks that are unused prefetches
//   v3 (all off by default) -- fixes found in the 22-mix / 20-mix evaluation of v2.x:
//           COORD_V_V3=1    acceptance rules.  A throttle step is kept only if fairness improves (score >= 1 + GAIN),
//                           throughput (mean IPC ratio) drops by at most COORD_V_AMTOL (0.005) and the throttled core
//                           itself loses at most COORD_V_SELFTOL (0.01) -- unless throughput rises by >= COORD_V_AMWIN
//                           (0.01).  Giving prefetching back is kept unless it costs throughput (> AMTOL).  Before, a
//                           throttle only had to raise the fairness score, so accurate, useful prefetchers (Pythia
//                           accuracy >= 0.6, IPCP 0.2-0.6) were throttled 50-80% of the time and lost 2-10%.
//           COORD_V_PROTECT never start a throttle trial on a core whose prefetch accuracy is >= this (e.g. 0.75)
//           COORD_V_SKIP    the first SKIP epochs after a change are not scored (transient: a throttled core's loss
//                           shows up late, its neighbours' gain immediately)
//           COORD_V_WAYS=5  partition = UCP's lookahead on demand hits + PSR x prefetch hits (PSR = accuracy x
//                           timeliness), installed every COORD_V_WPERIOD epochs like UCP (no trial, no churn).
//   v3.1 (with COORD_V_V3=1):
//           - every throttle is re-justified periodically: DRAM calm for COORD_V_CALM epochs -> every throttled core
//             gets one level back at once; a core throttled for its revalidation interval (COORD_V_RETEST, doubling
//             after each rejected revalidation up to COORD_V_RETESTMAX, reset by any kept change) gets an up-trial,
//             kept unless it costs throughput, and revalidations go before new throttle trials
//           - accuracy / timeliness / traffic keep being measured while a core is throttled (only issued requests
//             count: a dropped request is never filled, so it is neither useful, useless nor late)
//           - prefetcher-agnostic gate at CACHE::prefetch_line: of the requests one trigger issues in a cycle (a
//             "burst", issued nearest first by IPCP / Pythia / stride-type prefetchers) the FARTHEST are dropped:
//             level 2 keeps the first half, level 1 the first quarter (avg burst size; single-request streams are
//             thinned by a carried fraction), level 0 none -- a degree cut without touching the prefetcher
//           - after a kept trial the reference IPC is the trial mean; after a reverted one the old reference stays
//   v3.2:   COORD_V_UPSELF  an up-trial (revalidation / calm) is also reverted if the re-enabled core itself loses
//                           more than this: a prefetcher that hurts its own core stays throttled even when the
//                           throughput change is within noise (v3 re-enabled such cores about every other retest)
//           COORD_V_CALMSTEP=1  a calm-DRAM release gives back one level per COORD_V_CALM calm epochs (gradual
//                           0 -> 1 -> 2 -> 3, each level then subject to the self-tested trials), not one per epoch
//           COORD_V_PHASE   phase change: a throttled core whose IPC moved by more than this fraction since its level
//                           last changed is revalidated immediately (interval reset), so a core whose prefetching
//                           becomes useful again ramps back up level by level without waiting for the back-off
//           Prefetching is never switched off in one step: each level down (two for accuracy < COORD_V_DROP2) is a
//           separate trial that the throttled core itself must not lose by; every level back up is a separate trial.
//           - a direct partition install (WAYS=5) takes its epoch: no throttle trial starts on top of it
//           COORD_V_PSRFULL prefetch hits count fully once PSR >= this (weight min(1, PSR/PSRFULL)).  Use 0.3: useful
//                           IPCP prefetchers have accuracy 0.2-0.6 (PSR ~0.42) yet speed their core up 1.5x, while the
//                           harmful ones sit at accuracy ~0.1 (PSR ~0.10); timeliness is ~0.9 for all.
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
bool coord_pf_allow(uint32_t cpu, uint8_t cache_type, uint64_t base_addr);
int  coord_level(uint32_t cpu); // current prefetch level of a core (for prefetcher adapters)
void coord_final_stats();       // end-of-run summary (victim policy)

#endif /* COORD_H */
