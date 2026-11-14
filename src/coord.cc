#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>

#include "coord.h"
#include "ooo_cpu.h"
#include "uncore.h"

// Optional: current UCP way quota of a core. Defined by replacement/ucp.llc_repl; weak so that builds with another
// LLC replacement policy still link (the pointer is then null and the column is reported as 0).
extern uint32_t ucp_quota_of(uint32_t cpu) __attribute__((weak));
extern void ucp_set_external_control(bool on) __attribute__((weak));
extern bool ucp_set_quota(const uint32_t *q) __attribute__((weak));
extern void ucp_umon_hits(uint32_t cpu, uint64_t *dm, uint64_t *pf) __attribute__((weak));
extern void ucp_umon_decay() __attribute__((weak));

bool coord_armed = false;
bool coord_gate_active = false;

namespace {

enum Policy { POLICY_OFF = 0, POLICY_LOG = 1, POLICY_STATIC = 2, POLICY_VICTIM = 3 };

Policy   policy       = POLICY_OFF;
uint64_t epoch_cycles = 1000000;
uint64_t next_epoch   = 0;
uint64_t epoch_index  = 0;

// actuator state
uint8_t  level[NUM_CPUS];                 // prefetch level per core, COORD_LEVELS-1 = native
uint8_t  credit[NUM_CPUS][16];            // deterministic credit accumulators (per core, per cache type)
uint64_t throttled[NUM_CPUS];             // requests dropped by the throttle (cumulative)
bool     static_ways_set = false;
uint32_t static_ways[NUM_CPUS];

// cumulative counters of one private cache, as exposed by CACHE
struct PfCounters {
    uint64_t requested, issued, filled, useful, useless, late;
};

struct CoreSnap {
    uint64_t retired, cycles;
    PfCounters l1d, l2c;
    uint64_t llc_dacc, llc_dmiss;   // demand (LOAD + RFO) accesses / misses at the shared LLC, this core
    uint64_t llc_pfacc, llc_pfmiss; // prefetch accesses / misses at the shared LLC, this core
    uint64_t throttled;             // prefetch requests dropped by the coord throttle
};

struct Snap {
    CoreSnap core[NUM_CPUS];
    uint64_t dram_enqueue;
};

Snap last_snap;
bool quiet = false;

// ---- victim-aware, partition-aware prefetch throttling (POLICY_VICTIM) -----------------------------------------
struct VParams {
    double   bw_hi = 1.5, gain = 0.005, share = 0.5, alpha = 0.3;
    bool     part = false;                // COORD_V_PART=1: use UCP's way allocation (see coord.h)
    double   bw_lo = -1.0;                // COORD_V_BWLO: DRAM is "calm" below this (default: below bw_hi)
    double   upgain = -1.0;               // COORD_V_UPGAIN: keep an up-trial if score >= 1 + upgain (default -gain/2)
    bool     score_am = false;            // COORD_V_SCORE=am: arithmetic mean (throughput) instead of harmonic
    uint32_t trial = 2, cooldown = 16, calm = 4, retest = 32;
    // ---- two-way coordination (see coord.h): the controller also owns the LLC partition
    uint32_t ways = 0;                    // COORD_V_WAYS: 0 = UCP partitions by itself, 1 = accuracy-weighted utility,
                                          //               2 = UCP's own utility (demand + prefetch hits), both trialled
    bool     snap = false;                // COORD_V_SNAP=1: throttle prefetch-inflated way gainers, restore last-good state
    uint32_t wperiod = 5, wtrial = 2, wcool = 3;
    double   wtol = -9.0;                 // a repartition is kept unless the score drops below 1 - wtol (default gain)
    double   acc_lo = 0.4;                // prefetch accuracy below which a core's way gain counts as prefetch-inflated
    // ---- v2.1
    bool     fix = false;                 // COORD_V_FIX=1: a restore cools down only the offending core (not every core);
                                          // after a reverted way trial no way trial for COORD_V_WCOOL periods
    uint32_t wmin = 1;                    // COORD_V_WMIN: trial a new allocation only if it moves >= this many ways
    double   psr_hi = 0.6, psr_lo = 0.3;  // COORD_V_PSRHI / COORD_V_PSRLO: prefetch-success-rate tiers (COORD_V_WAYS=3)
    double   grant_hi = 0.8, grant_mid = 0.4, donor_frac = 1.0 / 3.0;
    // ---- v2.2 (all off by default)
    uint32_t wtrig = 0;                   // COORD_V_WTRIG: mode 4 acts only if prefetch-won ways >= this (else UCP's split)
    uint32_t wstep = 0;                   // COORD_V_WSTEP: move at most this many ways per period (0 = no limit)
    double   gamma = 1.0;                 // COORD_V_GAMMA: a requester keeps PSR^gamma of its prefetch-won ways
    double   guard = 0.0;                 // COORD_V_GUARD: reject a down/way trial if another core loses > guard
    double   drop2 = 0.0;                 // COORD_V_DROP2: accuracy below which a pressure throttle drops two levels
    bool     poll = false;                // COORD_V_POLL=1: rank self-harm suspects also by unused-prefetch LLC share
};
VParams  vp;
double   bw_acc = 0.0;                    // sum of the DRAM bandwidth level over the cycles of the epoch
uint64_t bw_n = 0;
bool     ref_ok = false;
double   ref_ipc[NUM_CPUS];               // recent IPC of every core (EWMA over epochs without a trial)
double   cost_ewma[NUM_CPUS];             // useless prefetch DRAM reads per kilo-cycle (EWMA): self-harm suspects
double   traf_ewma[NUM_CPUS];             // all prefetch DRAM reads per kilo-cycle (EWMA): bandwidth hogs (neighbour harm)
uint32_t rank_turn = 0;                   // alternates the two candidate rankings
double   acc_ewma[NUM_CPUS];              // prefetch accuracy (EWMA)
uint32_t cooldown_left[NUM_CPUS];
bool     in_trial = false;
int      trial_core = -1, trial_dir = 0;
uint32_t trial_left = 0, trial_n = 0;
double   trial_score = 0.0, trial_self = 0.0;
uint32_t calm_epochs = 0, since_retest = 0;
uint64_t n_down_ok = 0, n_down_rej = 0, n_up_ok = 0, n_up_rej = 0;
uint64_t level_epochs[NUM_CPUS][COORD_LEVELS];

// two-way coordination state
enum TrialKind { T_PF = 0, T_WAYS = 1 };
TrialKind trial_kind = T_PF;
bool     trial_rollback = false;          // the running prefetch trial throttles a prefetch-inflated way gainer
uint32_t cur_q[NUM_CPUS], prev_q[NUM_CPUS], rej_q[NUM_CPUS];
bool     rej_valid = false;
uint32_t wcool_left = 0, since_part = 0;
double   ref_before[NUM_CPUS];            // reference IPC before a way trial (restored when the trial is reverted)
int      pending_gainer = -1;
uint8_t  good_level[NUM_CPUS];            // last-good snapshot: prefetch levels and way allocation
uint32_t good_q[NUM_CPUS];
bool     good_valid = false;
uint64_t n_w_ok = 0, n_w_rej = 0, n_w_same = 0, n_gainer = 0, n_restore = 0;
uint64_t quota_epochs[NUM_CPUS];
double   tim_ewma[NUM_CPUS];              // prefetch timeliness: on-time useful / (useful + late), EWMA
uint32_t wblock_left = 0;                 // v2.1: periods left without way trials after a revert
uint64_t n_grant = 0, n_poll = 0;         // PSR mode: ways granted to prefetch-successful cores, polluter throttles
int      coupled_core = -1;               // v2.2: core throttled together with the running way trial (coupled move)
uint8_t  coupled_old = 0;
double   trial_rsum[NUM_CPUS];            // per-core IPC ratio summed over the running trial (victim guard)
double   poll_share[NUM_CPUS];            // share of a core's LLC blocks that are prefetched and still unused
uint64_t n_reclaim = 0, n_keepucp = 0, n_coupled = 0, n_coupled_ok = 0, n_guard = 0, n_drop2 = 0;

PfCounters read_pf(const CACHE &c)
{
    return PfCounters{c.pf_requested, c.pf_issued, c.pf_filled, c.pf_useful, c.pf_useless, c.pf_late};
}

void take(Snap &s)
{
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        CoreSnap &c = s.core[i];
        c.retired = ooo_cpu[i].num_retired;
        c.cycles  = current_core_cycle[i];
        c.l1d     = read_pf(ooo_cpu[i].L1D);
        c.l2c     = read_pf(ooo_cpu[i].L2C);
        const CACHE &llc = uncore.LLC;
        c.llc_dacc   = llc.sim_access[i][LOAD] + llc.sim_access[i][RFO];
        c.llc_dmiss  = llc.sim_miss[i][LOAD]   + llc.sim_miss[i][RFO];
        c.llc_pfacc  = llc.sim_access[i][PREFETCH];
        c.llc_pfmiss = llc.sim_miss[i][PREFETCH];
        c.throttled  = throttled[i];
    }
    s.dram_enqueue = uncore.DRAM.rq_enqueue_count;
}

void print_pf(const char *tag, const PfCounters &now, const PfCounters &old)
{
    std::printf(" %s_req=%llu %s_iss=%llu %s_fill=%llu %s_use=%llu %s_unused=%llu %s_late=%llu",
                tag, (unsigned long long)(now.requested - old.requested),
                tag, (unsigned long long)(now.issued    - old.issued),
                tag, (unsigned long long)(now.filled    - old.filled),
                tag, (unsigned long long)(now.useful    - old.useful),
                tag, (unsigned long long)(now.useless   - old.useless),
                tag, (unsigned long long)(now.late      - old.late));
}


// Parse "v" (broadcast to all cores) or "v0-v1-...-v{NUM_CPUS-1}". Returns false on any malformed input.
bool parse_per_core(const char *text, uint32_t *out)
{
    uint32_t n = 0;
    const char *p = text;
    while (*p) {
        if (*p < '0' || *p > '9' || n >= NUM_CPUS)
            return false;
        char *end = nullptr;
        out[n++] = (uint32_t)std::strtoul(p, &end, 10);
        p = end;
        if (*p == '-') {
            ++p;
            if (*p == '\0')
                return false;
        } else if (*p != '\0') {
            return false;
        }
    }
    if (n == 1)
        for (uint32_t i = 1; i < NUM_CPUS; i++)
            out[i] = out[0];
    else if (n != NUM_CPUS)
        return false;
    return true;
}

[[noreturn]] void die(const char *msg)
{
    std::fprintf(stderr, "[COORD] %s\n", msg);
    std::exit(1);
}

void update_gate()
{
    coord_gate_active = false;
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        if (level[i] < COORD_LEVELS - 1)
            coord_gate_active = true;
}

void start_trial(int j, int dir, const char *why, double bwavg)
{
    level[j] = (uint8_t)((int)level[j] + dir);
    in_trial = true;
    trial_kind = T_PF;
    trial_core = j;
    trial_dir = dir;
    trial_left = vp.trial;
    trial_n = 0;
    trial_score = trial_self = 0.0;
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        trial_rsum[i] = 0.0;
    update_gate();
    std::printf("COORD_V e=%llu trial core=%d %s to level %u (%s, bw=%.2f, useless=%.3f, traffic=%.3f, acc=%.3f)\n",
                (unsigned long long)epoch_index, j, dir < 0 ? "down" : "up", (unsigned)level[j], why, bwavg,
                cost_ewma[j], traf_ewma[j], acc_ewma[j]);
}

bool same_q(const uint32_t *a, const uint32_t *b)
{
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        if (a[i] != b[i])
            return false;
    return true;
}

void install_q(const uint32_t *q)
{
    if (!ucp_set_quota(q))
        die("ucp rejected a controller way allocation");
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        cur_q[i] = q[i];
}

void save_good()
{
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        good_level[i] = level[i];
        good_q[i] = cur_q[i];
    }
    good_valid = true;
}

// UCP's greedy lookahead allocation (one guaranteed way per core) on a given utility curve u[c][r] = hits at LRU
// position r.  Same algorithm as replacement/ucp.llc_repl, so with u = demand + prefetch hits it reproduces UCP.
void lookahead(double u[NUM_CPUS][LLC_WAY], uint32_t *q)
{
    uint32_t balance = LLC_WAY - NUM_CPUS;
    for (uint32_t c = 0; c < NUM_CPUS; c++)
        q[c] = 1;
    while (balance) {
        uint32_t winner = 0, requested = 0;
        double best_global = 0;
        for (uint32_t c = 0; c < NUM_CPUS; c++) {
            double total = 0, best_local = 0;
            uint32_t best_ways = 0;
            for (uint32_t w = q[c] + 1; w <= q[c] + balance; w++) {
                total += u[c][w - 1];
                const double marginal = total / (w - q[c]);
                if (marginal > best_local) {
                    best_local = marginal;
                    best_ways = w;
                }
            }
            if (best_local > best_global) {
                best_global = best_local;
                winner = c;
                requested = best_ways;
            }
        }
        if (requested <= q[winner])
            break;
        balance -= requested - q[winner];
        q[winner] = requested;
    }
    while (balance) {
        uint32_t least = 0;
        for (uint32_t c = 1; c < NUM_CPUS; c++)
            if (q[c] < q[least])
                least = c;
        ++q[least];
        --balance;
    }
}

// Weighted harmonic (or arithmetic) mean of the cores' IPC relative to ref_ipc; weights 1 + ways/avg (V_PART).
double score_of(const double *ipc, const double *ways, double avgw)
{
    double inv = 0.0, wsum = 0.0;
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        const double r = ref_ipc[i] > 0 ? ipc[i] / ref_ipc[i] : 1.0;
        const double w = vp.part ? 1.0 + ways[i] / avgw : 1.0;
        inv += vp.score_am ? w * r : w / (r > 1e-3 ? r : 1e-3);
        wsum += w;
    }
    return vp.score_am ? inv / wsum : wsum / inv;
}

// Prefetch success rate of a core = accuracy x timeliness.
double psr_of(uint32_t c)
{
    const double a = acc_ewma[c] < 0 ? 0 : acc_ewma[c] > 1 ? 1 : acc_ewma[c];
    const double t = tim_ewma[c] < 0 ? 0 : tim_ewma[c] > 1 ? 1 : tim_ewma[c];
    return a * t;
}

// COORD_V_WAYS=3, PSR-guided allocation.
//   defaultAlloc  = UCP lookahead on DEMAND hits only (what a demand-only UCP would choose)
//   prefetchAlloc = UCP lookahead on demand + prefetch hits (what UCP chooses when it sees prefetches)
// Start from defaultAlloc.  Cores that prefetchAlloc gives more ways ("requesters") get a share of the extra ways by
// their prefetch success rate PSR = accuracy x timeliness: >= psr_hi -> grant_hi (80%) of the extra ways, >= psr_lo
// -> grant_mid (40%), below -> none.  The ways come from donors (cores prefetchAlloc gives no more ways than
// defaultAlloc), each giving at most donor_frac (1/3) of its defaultAlloc ways and keeping >= 1 way; a way is always
// taken from the donor that loses the fewest DEMAND hits by giving it up (the least cache-sensitive donor).
// A requester with PSR < psr_lo is a polluter: its prefetches won it ways it cannot use -> *polluter (throttle it).
void psr_alloc(double dmu[NUM_CPUS][LLC_WAY], double pfu[NUM_CPUS][LLC_WAY], uint32_t *q, int *polluter)
{
    double both[NUM_CPUS][LLC_WAY];
    for (uint32_t c = 0; c < NUM_CPUS; c++)
        for (uint32_t r = 0; r < LLC_WAY; r++)
            both[c][r] = dmu[c][r] + pfu[c][r];
    uint32_t dA[NUM_CPUS], pA[NUM_CPUS], budget[NUM_CPUS];
    lookahead(dmu, dA);
    lookahead(both, pA);
    for (uint32_t c = 0; c < NUM_CPUS; c++) {
        q[c] = dA[c];
        budget[c] = 0;
        if (pA[c] <= dA[c] && dA[c] > 1) {
            const uint32_t b = (uint32_t)(dA[c] * vp.donor_frac);
            budget[c] = b < dA[c] - 1 ? b : dA[c] - 1;
        }
    }
    *polluter = -1;
    double pworst = 0.0;
    bool done[NUM_CPUS] = {};
    for (;;) {                                         // requesters in order of PSR, best first
        int i = -1;
        for (uint32_t c = 0; c < NUM_CPUS; c++)
            if (!done[c] && pA[c] > dA[c] && (i < 0 || psr_of(c) > psr_of((uint32_t)i)))
                i = (int)c;
        if (i < 0)
            break;
        done[i] = true;
        const double psr = psr_of((uint32_t)i);
        const uint32_t need = pA[i] - dA[i];
        const double f = psr >= vp.psr_hi ? vp.grant_hi : psr >= vp.psr_lo ? vp.grant_mid : 0.0;
        if (f == 0.0) {
            const double m = (double)need * (1.0 - psr);
            if (level[i] > 0 && !cooldown_left[i] && m > pworst) {
                pworst = m;
                *polluter = i;
            }
            continue;
        }
        uint32_t want = (uint32_t)(f * need + 0.5);
        while (want > 0) {
            int d = -1;
            for (uint32_t c = 0; c < NUM_CPUS; c++)
                if (budget[c] && q[c] > 1 && (d < 0 || dmu[c][q[c] - 1] < dmu[d][q[d] - 1]))
                    d = (int)c;
            if (d < 0)
                break;
            q[d]--;
            budget[d]--;
            q[i]++;
            want--;
            n_grant++;
        }
    }
}

// Victim guard: no other core may lose more than COORD_V_GUARD (mean IPC ratio over the trial) -- protects per-core
// slowdown even when the mix average improves.
bool guard_ok(int exclude)
{
    if (vp.guard <= 0.0 || trial_n == 0)
        return true;
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        if ((int)i != exclude && trial_rsum[i] / trial_n < 1.0 - vp.guard)
            return false;
    return true;
}

// COORD_V_WAYS=4, reclaim mode (v2.2).  Start from UCP's own split (prefetchAlloc: demand + prefetch hits) and take
// back only the ways a core cannot justify: a core that prefetchAlloc gives more ways than defaultAlloc (demand hits
// only) keeps PSR^gamma of those prefetch-won ways; the reclaimed ways go, one at a time, to the core whose next way
// has the most DEMAND hits (UCP's marginal-utility rule).  With accurate, timely prefetchers nothing is reclaimed and
// the split equals UCP's.  The partition is only touched when prefetch-won ways >= COORD_V_WTRIG.  A requester with
// PSR < COORD_V_PSRLO is a polluter: it is throttled in the same trial (coupled move).
void reclaim_alloc(double dmu[NUM_CPUS][LLC_WAY], double pfu[NUM_CPUS][LLC_WAY], uint32_t *q, int *polluter)
{
    double both[NUM_CPUS][LLC_WAY];
    for (uint32_t c = 0; c < NUM_CPUS; c++)
        for (uint32_t r = 0; r < LLC_WAY; r++)
            both[c][r] = dmu[c][r] + pfu[c][r];
    uint32_t dA[NUM_CPUS], pA[NUM_CPUS];
    lookahead(dmu, dA);
    lookahead(both, pA);
    *polluter = -1;
    uint32_t won = 0;
    for (uint32_t c = 0; c < NUM_CPUS; c++) {
        q[c] = pA[c];
        won += pA[c] > dA[c] ? pA[c] - dA[c] : 0;
    }
    if (won < vp.wtrig || won == 0) {
        n_keepucp++;
        return;
    }
    uint32_t freed = 0;
    bool from[NUM_CPUS] = {};
    double pworst = 0.0;
    for (uint32_t c = 0; c < NUM_CPUS; c++) {
        if (pA[c] <= dA[c])
            continue;
        const uint32_t extra = pA[c] - dA[c];
        const double psr = psr_of(c);
        uint32_t keep = (uint32_t)(std::pow(psr, vp.gamma) * extra + 0.5);
        if (keep > extra)
            keep = extra;
        const uint32_t rec = extra - keep;
        if (rec) {
            q[c] -= rec;
            freed += rec;
            from[c] = true;
        }
        if (psr < vp.psr_lo && level[c] > 0 && !cooldown_left[c] && extra * (1.0 - psr) > pworst) {
            pworst = extra * (1.0 - psr);
            *polluter = (int)c;
        }
    }
    n_reclaim += freed;
    while (freed) {
        int b = -1;
        for (uint32_t pass = 0; pass < 2 && b < 0; pass++)
            for (uint32_t c = 0; c < NUM_CPUS; c++)
                if ((pass || !from[c]) && q[c] < LLC_WAY && (b < 0 || dmu[c][q[c]] > dmu[b][q[b]]))
                    b = (int)c;
        q[b]++;
        freed--;
    }
}

// Move from the current split towards target q by at most COORD_V_WSTEP ways (largest surplus -> largest deficit).
void step_limit(uint32_t *q)
{
    if (!vp.wstep)
        return;
    uint32_t t[NUM_CPUS];
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        t[i] = cur_q[i];
    for (uint32_t k = 0; k < vp.wstep; k++) {
        int a = -1, b = -1;
        for (uint32_t i = 0; i < NUM_CPUS; i++) {
            if (t[i] > q[i] && (a < 0 || t[i] - q[i] > t[a] - q[a]))
                a = (int)i;
            if (q[i] > t[i] && (b < 0 || q[i] - t[i] > q[b] - t[b]))
                b = (int)i;
        }
        if (a < 0 || b < 0)
            break;
        t[a]--;
        t[b]++;
    }
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        q[i] = t[i];
}

// Partition step (every COORD_V_WPERIOD epochs outside trials): the controller's own allocation from the split
// utility monitor.  Mode 1 counts a core's prefetch-request hits in proportion to its prefetch accuracy, so an
// inaccurate prefetcher cannot inflate its core's utility and take ways from cache-sensitive neighbours.
// A changed allocation is installed as a trial and kept only if the system score does not drop.
bool ways_step(double bwavg)
{
    double u[NUM_CPUS][LLC_WAY], dmu[NUM_CPUS][LLC_WAY], pfu[NUM_CPUS][LLC_WAY];
    for (uint32_t c = 0; c < NUM_CPUS; c++) {
        uint64_t dm[LLC_WAY], pf[LLC_WAY];
        ucp_umon_hits(c, dm, pf);
        const double a = vp.ways == 1 ? (acc_ewma[c] < 0 ? 0 : acc_ewma[c] > 1 ? 1 : acc_ewma[c]) : 1.0;
        for (uint32_t r = 0; r < LLC_WAY; r++) {
            u[c][r] = (double)dm[r] + a * (double)pf[r];
            dmu[c][r] = (double)dm[r];
            pfu[c][r] = (double)pf[r];
        }
    }
    ucp_umon_decay();
    if (std::getenv("COORD_V_DEBUG")) {                // shadow-tag hit totals per core: demand / prefetch
        std::printf("COORD_U e=%llu", (unsigned long long)epoch_index);
        for (uint32_t c = 0; c < NUM_CPUS; c++) {
            double d = 0, f = 0;
            for (uint32_t r = 0; r < LLC_WAY; r++) {
                d += dmu[c][r];
                f += pfu[c][r];
            }
            std::printf(" c%u=%.0f/%.0f", c, d, f);
        }
        std::printf("\n");
    }
    uint32_t q[NUM_CPUS];
    int pol4 = -1;
    if (vp.ways == 4) {
        reclaim_alloc(dmu, pfu, q, &pol4);
        step_limit(q);
    } else if (vp.ways == 3) {
        int polluter = -1;
        psr_alloc(dmu, pfu, q, &polluter);
        step_limit(q);                                 // COORD_V_WSTEP (0 = off) also limits the PSR rule's moves
        if (polluter >= 0 && pending_gainer < 0) {     // throttle the polluter next (as a normal keep-or-revert trial)
            pending_gainer = polluter;
            n_poll++;
        }
    } else {
        lookahead(u, q);
    }
    auto no_way_trial = [&]() {                        // mode 4: the polluter is still throttled (plain trial)
        if (pol4 >= 0 && pending_gainer < 0) {
            pending_gainer = pol4;
            n_poll++;
        }
        return false;
    };
    if (wcool_left)
        wcool_left--;
    if (same_q(q, cur_q)) {
        n_w_same++;
        return no_way_trial();
    }
    if (vp.fix) {
        if (wblock_left) {                             // recently reverted: let the partition settle
            wblock_left--;
            return no_way_trial();
        }
        uint32_t moved = 0;
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            moved += q[i] > cur_q[i] ? q[i] - cur_q[i] : 0;
        if (moved < vp.wmin) {                         // too small to be worth a trial (and the line migration)
            n_w_same++;
            return no_way_trial();
        }
    }
    if (rej_valid && wcool_left && same_q(q, rej_q))
        return no_way_trial();
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        prev_q[i] = cur_q[i];
        ref_before[i] = ref_ipc[i];
    }
    install_q(q);
    in_trial = true;
    trial_kind = T_WAYS;
    trial_left = vp.wtrial;
    trial_n = 0;
    trial_score = 0.0;
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        trial_rsum[i] = 0.0;
    coupled_core = -1;
    if (pol4 >= 0) {                                   // coupled move: throttle the polluter in the same trial
        coupled_core = pol4;
        coupled_old = level[pol4];
        const int d = (vp.drop2 > 0 && acc_ewma[pol4] < vp.drop2 && level[pol4] >= 2) ? 2 : 1;
        level[pol4] = (uint8_t)(level[pol4] - d);
        update_gate();
        n_coupled++;
        n_poll++;
    }
    std::printf("COORD_W e=%llu trial%s ways", (unsigned long long)epoch_index, coupled_core >= 0 ? " coupled" : "");
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        std::printf("%s%u", i ? "-" : " ", prev_q[i]);
    std::printf(" ->");
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        std::printf("%s%u", i ? "-" : " ", q[i]);
    if (coupled_core >= 0)
        std::printf(" + core %d level %u->%u (psr=%.3f)", coupled_core, (unsigned)coupled_old, (unsigned)level[coupled_core],
                    psr_of((uint32_t)coupled_core));
    std::printf(" (bw=%.2f)\n", bwavg);
    return true;
}

// One decision step at the end of an epoch (deltas between o and n).
void victim_step(const Snap &n, const Snap &o, double bwavg)
{
    if (vp.poll) {                                     // unused-prefetch share of each core's LLC blocks
        uint64_t tot[NUM_CPUS] = {}, unused[NUM_CPUS] = {};
        for (uint32_t st = 0; st < LLC_SET; st++)
            for (uint32_t w = 0; w < LLC_WAY; w++) {
                const BLOCK &b = uncore.LLC.block[st][w];
                if (b.valid && b.cpu < NUM_CPUS) {
                    tot[b.cpu]++;
                    if (b.prefetch)
                        unused[b.cpu]++;
                }
            }
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            poll_share[i] = tot[i] ? (double)unused[i] / (double)tot[i] : 0.0;
    }
    double ipc[NUM_CPUS];
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        const CoreSnap &a = n.core[i], &b = o.core[i];
        const uint64_t dins = a.retired - b.retired, dcyc = a.cycles - b.cycles;
        ipc[i] = dcyc ? (double)dins / (double)dcyc : 0.0;
        const double use = (double)(a.l1d.useful - b.l1d.useful + a.l2c.useful - b.l2c.useful);
        const double unu = (double)(a.l1d.useless - b.l1d.useless + a.l2c.useless - b.l2c.useless);
        const double acc = use + unu > 0 ? use / (use + unu) : 1.0;
        const double traf = (double)(a.llc_pfmiss - b.llc_pfmiss) * 1000.0 / (double)epoch_cycles;
        const double cost = traf * (1.0 - acc);
        if (level[i] == COORD_LEVELS - 1 || !ref_ok) {        // judge a core by its unthrottled behaviour
            cost_ewma[i] = ref_ok ? vp.alpha * cost + (1 - vp.alpha) * cost_ewma[i] : cost;
            traf_ewma[i] = ref_ok ? vp.alpha * traf + (1 - vp.alpha) * traf_ewma[i] : traf;
            if (use + unu > 0)
                acc_ewma[i] = ref_ok ? vp.alpha * acc + (1 - vp.alpha) * acc_ewma[i] : acc;
            const double late = (double)(a.l1d.late - b.l1d.late + a.l2c.late - b.l2c.late);
            if (use + late > 0) {
                const double tim = use / (use + late);
                tim_ewma[i] = ref_ok ? vp.alpha * tim + (1 - vp.alpha) * tim_ewma[i] : tim;
            }
        }
        level_epochs[i][level[i]]++;
        if (vp.ways)
            quota_epochs[i] += cur_q[i];
    }
    since_part++;
    calm_epochs = bwavg >= (vp.bw_lo >= 0 ? vp.bw_lo : vp.bw_hi) ? 0 : calm_epochs + 1;
    if (!ref_ok) {
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            ref_ipc[i] = ipc[i];
        ref_ok = true;
        return;
    }
    // partition input: UCP's current ways per core (cache sensitivity as judged by UCP's utility monitors)
    double ways[NUM_CPUS], avgw = (double)LLC_WAY / NUM_CPUS;
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        ways[i] = (vp.part && ucp_quota_of) ? (double)ucp_quota_of(i) : avgw;
    if (in_trial && trial_kind == T_WAYS) {
        double pw[NUM_CPUS];
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            pw[i] = (double)prev_q[i];                         // weights from the allocation before the trial
        trial_score += score_of(ipc, pw, avgw);
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            trial_rsum[i] += ref_ipc[i] > 0 ? ipc[i] / ref_ipc[i] : 1.0;
        trial_n++;
        if (--trial_left > 0)
            return;
        const double score = trial_score / trial_n;
        bool ok = score >= 1.0 - (vp.wtol > -5 ? vp.wtol : vp.gain);
        if (coupled_core >= 0)                         // a coupled move must also pay for the throttle
            ok = score >= 1.0 + vp.gain;
        if (ok && !guard_ok(coupled_core)) {
            ok = false;
            n_guard++;
        }
        in_trial = false;
        const int cc = coupled_core;
        coupled_core = -1;
        if (cc >= 0) {
            if (ok) {
                n_coupled_ok++;
                cooldown_left[cc] = vp.cooldown / 4;
            } else {
                level[cc] = coupled_old;
                cooldown_left[cc] = vp.cooldown;
            }
            update_gate();
        }
        if (ok) {
            n_w_ok++;
            for (uint32_t i = 0; i < NUM_CPUS; i++)
                ref_ipc[i] = ipc[i];
            if (vp.snap)
                save_good();
            std::printf("COORD_W e=%llu keep score=%.4f\n", (unsigned long long)epoch_index, score);
            return;
        }
        n_w_rej++;
        uint32_t tried[NUM_CPUS];
        for (uint32_t i = 0; i < NUM_CPUS; i++) {
            tried[i] = cur_q[i];
            rej_q[i] = cur_q[i];
            ref_ipc[i] = ref_before[i];
        }
        rej_valid = true;
        wcool_left = vp.wcool;
        if (vp.fix)
            wblock_left = vp.wcool;
        install_q(prev_q);
        // the partition change hurt: if it gave ways to a core whose prefetcher is inaccurate, the core's utility was
        // inflated by its own prefetches -> throttle that core next (rollback first, then the prefetch knob)
        int g = -1;
        double gbest = 0.0;
        if (vp.snap)
            for (uint32_t i = 0; i < NUM_CPUS; i++)
                if (tried[i] > prev_q[i] && acc_ewma[i] < vp.acc_lo && level[i] > 0) {
                    const double m = (double)(tried[i] - prev_q[i]) * (1.0 - acc_ewma[i]);
                    if (m > gbest) {
                        gbest = m;
                        g = (int)i;
                    }
                }
        pending_gainer = g;
        std::printf("COORD_W e=%llu revert score=%.4f gainer=%d\n", (unsigned long long)epoch_index, score, g);
        return;
    }
    if (in_trial) {
        double inv = 0.0, wsum = 0.0;
        for (uint32_t i = 0; i < NUM_CPUS; i++) {
            const double r = ref_ipc[i] > 0 ? ipc[i] / ref_ipc[i] : 1.0;
            const double w = vp.part ? 1.0 + ways[i] / avgw : 1.0;   // cores UCP protects count more
            inv += vp.score_am ? w * r : w / (r > 1e-3 ? r : 1e-3);
            wsum += w;
        }
        trial_score += vp.score_am ? inv / wsum : wsum / inv;  // (weighted) harmonic mean (fairness) or arithmetic
        trial_self += ref_ipc[trial_core] > 0 ? ipc[trial_core] / ref_ipc[trial_core] : 1.0;
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            trial_rsum[i] += ref_ipc[i] > 0 ? ipc[i] / ref_ipc[i] : 1.0;
        trial_n++;
        if (--trial_left > 0)
            return;
        const double score = trial_score / trial_n, self = trial_self / trial_n;
        bool ok = trial_dir < 0 ? score >= 1.0 + vp.gain
                                 : score >= 1.0 + (vp.upgain > -0.5 ? vp.upgain : -vp.gain / 2);
        const int j = trial_core;
        if (ok && trial_dir < 0 && !guard_ok(j)) {             // v2.2 victim guard (down trials only)
            ok = false;
            n_guard++;
        }
        if (ok) {
            (trial_dir < 0 ? n_down_ok : n_up_ok)++;
            cooldown_left[j] = trial_dir < 0 ? vp.cooldown / 4 : 0;
            if (vp.snap)
                save_good();
        } else {
            (trial_dir < 0 ? n_down_rej : n_up_rej)++;
            level[j] = (uint8_t)((int)level[j] - trial_dir);
            cooldown_left[j] = vp.cooldown;
        }
        std::printf("COORD_V e=%llu %s core=%d level=%u score=%.4f self=%.4f (down %llu/%llu up %llu/%llu kept)\n",
                    (unsigned long long)epoch_index, ok ? "keep" : "revert", j, (unsigned)level[j], score, self,
                    (unsigned long long)n_down_ok, (unsigned long long)(n_down_ok + n_down_rej),
                    (unsigned long long)n_up_ok, (unsigned long long)(n_up_ok + n_up_rej));
        in_trial = false;
        const bool restore = !ok && trial_rollback && vp.snap && good_valid;
        trial_rollback = false;
        if (restore) {
            // neither the way rollback nor throttling the gainer helped: go back to the last-good joint setting
            n_restore++;
            for (uint32_t i = 0; i < NUM_CPUS; i++) {
                level[i] = good_level[i];
                if (!vp.fix || (int)i == j)                    // v2.1: only the offending core waits
                    cooldown_left[i] = vp.cooldown;
            }
            if (!same_q(good_q, cur_q))
                install_q(good_q);
            update_gate();
            ref_ok = false;                                    // re-learn the reference under the restored setting
            std::printf("COORD_W e=%llu restore last-good snapshot\n", (unsigned long long)epoch_index);
            return;
        }
        update_gate();
        for (uint32_t i = 0; i < NUM_CPUS; i++)               // new reference = behaviour with the decision applied
            ref_ipc[i] = ipc[i];
        return;
    }
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        ref_ipc[i] = vp.alpha * ipc[i] + (1 - vp.alpha) * ref_ipc[i];
        if (cooldown_left[i])
            cooldown_left[i]--;
    }
    since_retest++;
    if (pending_gainer >= 0) {
        const int g = pending_gainer;
        pending_gainer = -1;
        if (level[g] > 0) {
            n_gainer++;
            start_trial(g, -1, vp.ways >= 3 ? "polluter/low-PSR-way-request" : "rollback/prefetch-inflated-utility", bwavg);
            trial_rollback = vp.snap;
            return;
        }
    }
    if (vp.ways && since_part >= vp.wperiod) {
        since_part = 0;
        if (ways_step(bwavg))
            return;
    }
    if (bwavg >= vp.bw_hi) {
        // Pressure: two kinds of suspects, tried in turn -- (a) self-harm: most USELESS prefetch DRAM traffic
        // (inaccurate prefetchers, e.g. IPCP on irregular cache-sensitive code); (b) neighbour harm: most prefetch
        // DRAM traffic of any kind (accurate streamers that eat the bandwidth the partition-protected cores need).
        // The trial itself decides whether throttling helps the system.
        for (uint32_t k = 0; k < 2; k++) {
            const bool useless_rank = ((rank_turn + k) % 2 == 0);
            double m[NUM_CPUS];
            for (uint32_t i = 0; i < NUM_CPUS; i++)            // bandwidth hogs: prefer cores UCP gave few ways
                m[i] = useless_rank ? cost_ewma[i] * (vp.poll ? 1.0 + poll_share[i] : 1.0)
                                    : traf_ewma[i] * (avgw / (ways[i] > 1.0 ? ways[i] : 1.0));
            double total = 0.0;
            int j = -1;
            for (uint32_t i = 0; i < NUM_CPUS; i++) {
                total += m[i];
                if (level[i] > 0 && !cooldown_left[i] && (j < 0 || m[i] > m[j]))
                    j = (int)i;
            }
            if (j >= 0 && total > 0 && m[j] >= vp.share * total / NUM_CPUS) {
                const bool useless = useless_rank;
                rank_turn++;
                const bool two = vp.drop2 > 0 && acc_ewma[j] < vp.drop2 && level[j] >= 2;
                if (two)
                    n_drop2++;
                start_trial(j, two ? -2 : -1, useless ? "pressure/useless-traffic" : "pressure/bandwidth-hog", bwavg);
                return;
            }
        }
    }
    int up = -1;                                               // calm, or time to re-test: give one level back
    if (calm_epochs >= vp.calm || since_retest >= vp.retest)
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            if (level[i] < COORD_LEVELS - 1 && !cooldown_left[i] && (up < 0 || acc_ewma[i] > acc_ewma[up]))
                up = (int)i;
    if (up >= 0) {
        since_retest = 0;
        start_trial(up, +1, calm_epochs >= vp.calm ? "calm" : "retest", bwavg);
    }
}

void epoch_boundary()
{
    Snap now;
    take(now);
    const double bwavg = bw_n ? bw_acc / (double)bw_n : 0.0;
    bw_acc = 0.0;
    bw_n = 0;
    if (policy == POLICY_VICTIM)
        victim_step(now, last_snap, bwavg);
    if (quiet) {
        last_snap = now;
        epoch_index++;
        return;
    }

    // one global line, then one line per core; all values are deltas over the epoch that just ended
    std::printf("COORD_G e=%llu cycle=%llu dram_bw=%u dram_bw_avg=%.3f dram_enq=%llu\n",
                (unsigned long long)epoch_index, (unsigned long long)uncore.cycle,
                (unsigned)uncore.DRAM.bw, bwavg, (unsigned long long)(now.dram_enqueue - last_snap.dram_enqueue));

    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        const CoreSnap &n = now.core[i], &o = last_snap.core[i];
        const uint64_t dins = n.retired - o.retired, dcyc = n.cycles - o.cycles;
        std::printf("COORD_C e=%llu c=%u ipc=%.4f ins=%llu", (unsigned long long)epoch_index, i,
                    dcyc ? (double)dins / (double)dcyc : 0.0, (unsigned long long)dins);
        print_pf("l1d", n.l1d, o.l1d);
        print_pf("l2c", n.l2c, o.l2c);
        std::printf(" lvl=%u thr=%llu", (unsigned)level[i], (unsigned long long)(n.throttled - o.throttled));
        std::printf(" llc_dacc=%llu llc_dmiss=%llu llc_pfacc=%llu llc_pfmiss=%llu ways=%u\n",
                    (unsigned long long)(n.llc_dacc - o.llc_dacc),   (unsigned long long)(n.llc_dmiss - o.llc_dmiss),
                    (unsigned long long)(n.llc_pfacc - o.llc_pfacc), (unsigned long long)(n.llc_pfmiss - o.llc_pfmiss),
                    ucp_quota_of ? ucp_quota_of(i) : 0u);
    }

    last_snap = now;
    epoch_index++;
}

} // namespace

void coord_init()
{
    const char *p = std::getenv("COORD_POLICY");
    if (p == nullptr || *p == '\0' || !std::strcmp(p, "off")) {
        policy = POLICY_OFF;
    } else if (!std::strcmp(p, "log")) {
        policy = POLICY_LOG;
    } else if (!std::strcmp(p, "static")) {
        policy = POLICY_STATIC;
    } else if (!std::strcmp(p, "victim")) {
        policy = POLICY_VICTIM;
    } else {
        std::fprintf(stderr, "[COORD] unknown COORD_POLICY '%s' (use off|log|static|victim)\n", p);
        std::exit(1);
    }
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        level[i] = COORD_LEVELS - 1;
    }
    if (const char *e = std::getenv("COORD_EPOCH_CYCLES")) {
        const long long n = std::atoll(e);
        if (n <= 0) {
            std::fprintf(stderr, "[COORD] COORD_EPOCH_CYCLES must be positive\n");
            std::exit(1);
        }
        epoch_cycles = (uint64_t)n;
    }

    if (const char *q = std::getenv("COORD_QUIET"))
        quiet = *q == '1';
    if (policy == POLICY_VICTIM) {
        auto num = [](const char *name, double &v) { if (const char *e = std::getenv(name)) v = std::atof(e); };
        auto cnt = [](const char *name, uint32_t &v) { if (const char *e = std::getenv(name)) v = (uint32_t)std::atoi(e); };
        num("COORD_V_BW", vp.bw_hi);
        num("COORD_V_GAIN", vp.gain);
        num("COORD_V_SHARE", vp.share);
        if (const char *e = std::getenv("COORD_V_PART"))
            vp.part = *e == '1';
        num("COORD_V_BWLO", vp.bw_lo);
        num("COORD_V_UPGAIN", vp.upgain);
        if (const char *e = std::getenv("COORD_V_SCORE"))
            vp.score_am = !std::strcmp(e, "am");
        if (vp.part && !ucp_quota_of)
            die("COORD_V_PART=1 needs the ucp LLC replacement policy");
        cnt("COORD_V_TRIAL", vp.trial);
        cnt("COORD_V_COOLDOWN", vp.cooldown);
        cnt("COORD_V_CALM", vp.calm);
        cnt("COORD_V_RETEST", vp.retest);
        cnt("COORD_V_WAYS", vp.ways);
        if (const char *e = std::getenv("COORD_V_SNAP"))
            vp.snap = *e == '1';
        cnt("COORD_V_WPERIOD", vp.wperiod);
        cnt("COORD_V_WTRIAL", vp.wtrial);
        cnt("COORD_V_WCOOL", vp.wcool);
        num("COORD_V_WTOL", vp.wtol);
        num("COORD_V_ACCLO", vp.acc_lo);
        if (const char *e = std::getenv("COORD_V_FIX"))
            vp.fix = *e == '1';
        cnt("COORD_V_WMIN", vp.wmin);
        num("COORD_V_PSRHI", vp.psr_hi);
        num("COORD_V_PSRLO", vp.psr_lo);
        num("COORD_V_GRANTHI", vp.grant_hi);
        num("COORD_V_GRANTMID", vp.grant_mid);
        num("COORD_V_DONOR", vp.donor_frac);
        cnt("COORD_V_WTRIG", vp.wtrig);
        cnt("COORD_V_WSTEP", vp.wstep);
        num("COORD_V_GAMMA", vp.gamma);
        num("COORD_V_GUARD", vp.guard);
        num("COORD_V_DROP2", vp.drop2);
        if (const char *e = std::getenv("COORD_V_POLL"))
            vp.poll = *e == '1';
        if (vp.ways > 4)
            die("COORD_V_WAYS must be 0..4");
        if (vp.ways && (!ucp_umon_hits || !ucp_umon_decay || !ucp_set_quota || !ucp_set_external_control))
            die("COORD_V_WAYS needs the ucp LLC replacement policy");
        if (vp.snap && !vp.ways)
            die("COORD_V_SNAP=1 needs COORD_V_WAYS=1 or 2");
        if (vp.wperiod < 1 || vp.wtrial < 1)
            die("COORD_V_WPERIOD and COORD_V_WTRIAL must be >= 1");
        if (vp.trial < 1)
            die("COORD_V_TRIAL must be >= 1");
    }
    if (policy == POLICY_STATIC) {
        if (const char *l = std::getenv("COORD_STATIC_LEVEL")) {
            uint32_t v[NUM_CPUS];
            if (!parse_per_core(l, v))
                die("COORD_STATIC_LEVEL must be one value or one value per core, e.g. 2 or 3-1-0-2");
            for (uint32_t i = 0; i < NUM_CPUS; i++) {
                if (v[i] >= COORD_LEVELS)
                    die("COORD_STATIC_LEVEL values must be 0..3");
                level[i] = (uint8_t)v[i];
                if (level[i] < COORD_LEVELS - 1)
                    coord_gate_active = true;
            }
        }
        if (const char *w = std::getenv("COORD_STATIC_WAYS")) {
            static_ways_set = true;
            if (!std::strcmp(w, "equal")) {
                for (uint32_t i = 0; i < NUM_CPUS; i++)
                    static_ways[i] = LLC_WAY / NUM_CPUS + (i < LLC_WAY % NUM_CPUS ? 1 : 0);  // same as UCP's initial split
            } else if (!parse_per_core(w, static_ways)) {
                die("COORD_STATIC_WAYS must be 'equal' or one value per core, e.g. 2-2-6-6");
            }
            uint32_t total = 0;
            for (uint32_t i = 0; i < NUM_CPUS; i++) {
                if (static_ways[i] < 1)
                    die("COORD_STATIC_WAYS: every core needs at least one way");
                total += static_ways[i];
            }
            if (total != LLC_WAY)
                die("COORD_STATIC_WAYS must sum to LLC_WAY");
            if (!ucp_set_quota || !ucp_set_external_control)
                die("COORD_STATIC_WAYS needs the ucp LLC replacement policy (PYTHIA_LLC_REPLACEMENT=ucp)");
        }
    }
}

void coord_arm()
{
    if (policy == POLICY_OFF)
        return;
    if (policy == POLICY_STATIC && static_ways_set) {
        ucp_set_external_control(true);
        if (!ucp_set_quota(static_ways))
            die("ucp rejected COORD_STATIC_WAYS");
    }
    if (policy == POLICY_VICTIM && vp.ways) {
        ucp_set_external_control(true);                        // the controller owns the partition from now on
        for (uint32_t i = 0; i < NUM_CPUS; i++) {
            cur_q[i] = ucp_quota_of(i);
            acc_ewma[i] = 1.0;                                 // unknown accuracy = full credit (way mode only)
            tim_ewma[i] = 1.0;
        }
        save_good();
    }
    take(last_snap);
    epoch_index = 0;
    next_epoch  = uncore.cycle + epoch_cycles;
    coord_armed = true;
    std::printf("[COORD] policy=%s epoch_cycles=%llu cores=%u first_boundary=%llu\n",
                policy == POLICY_STATIC ? "static" : policy == POLICY_VICTIM ? "victim" : "log",
                (unsigned long long)epoch_cycles, (unsigned)NUM_CPUS, (unsigned long long)next_epoch);
    if (policy == POLICY_VICTIM)
        std::printf("[COORD] victim: bw>=%.2f gain=%.4f share=%.2f trial=%u cooldown=%u calm=%u retest=%u part=%d "
                    "bwlo=%.2f upgain=%.4f score=%s\n", vp.bw_hi, vp.gain, vp.share, vp.trial, vp.cooldown, vp.calm,
                    vp.retest, (int)vp.part, vp.bw_lo, vp.upgain, vp.score_am ? "am" : "hm");
    if (policy == POLICY_VICTIM && vp.ways)
        std::printf("[COORD] v2.1: fix=%d wmin=%u psr_hi=%.2f psr_lo=%.2f grants=%.2f/%.2f donor=%.2f\n", (int)vp.fix,
                    vp.wmin, vp.psr_hi, vp.psr_lo, vp.grant_hi, vp.grant_mid, vp.donor_frac);
    if (policy == POLICY_VICTIM)
        std::printf("[COORD] v2.2: wtrig=%u wstep=%u gamma=%.2f guard=%.3f drop2=%.2f poll=%d\n", vp.wtrig, vp.wstep,
                    vp.gamma, vp.guard, vp.drop2, (int)vp.poll);
    if (policy == POLICY_VICTIM && vp.ways)
        std::printf("[COORD] ways: mode=%u (%s) snap=%d period=%u trial=%u cool=%u tol=%.4f acc_lo=%.2f\n", vp.ways,
                    vp.ways == 1 ? "accuracy-weighted utility" : vp.ways == 3 ? "PSR-guided" : vp.ways == 4 ? "reclaim (v2.2)" : "UCP utility", (int)vp.snap, vp.wperiod, vp.wtrial,
                    vp.wcool, vp.wtol > -5 ? vp.wtol : vp.gain, vp.acc_lo);
}

void coord_tick()
{
    if (policy == POLICY_VICTIM) {
        bw_acc += uncore.DRAM.bw;
        bw_n++;
    }
    if (uncore.cycle < next_epoch)
        return;
    epoch_boundary();
    next_epoch += epoch_cycles;
}

bool coord_pf_allow(uint32_t cpu, uint8_t cache_type)
{
    // only the private L1D / L2C prefetchers are throttled; level COORD_LEVELS-1 (native) never drops anything
    if (cpu >= NUM_CPUS || (cache_type != IS_L1D && cache_type != IS_L2C))
        return true;
    static const uint8_t quarters[COORD_LEVELS] = {0, 1, 2, 4};  // allowed requests per 4 attempts
    const uint8_t n = quarters[level[cpu]];
    if (n >= 4)
        return true;
    credit[cpu][cache_type] += n;
    if (credit[cpu][cache_type] >= 4) {
        credit[cpu][cache_type] -= 4;
        return true;
    }
    ++throttled[cpu];
    return false;
}

int coord_level(uint32_t cpu)
{
    return cpu < NUM_CPUS ? level[cpu] : COORD_LEVELS - 1;
}

// Summary printed once at the end of the simulation (called from print_dram_stats' caller in main.cc).
void coord_final_stats()
{
    if (policy != POLICY_VICTIM)
        return;
    std::printf("COORD_VSUM trials_down_kept=%llu trials_down=%llu trials_up_kept=%llu trials_up=%llu guard_rejects=%llu "
                "drop2=%llu\n",
                (unsigned long long)n_down_ok, (unsigned long long)(n_down_ok + n_down_rej),
                (unsigned long long)n_up_ok, (unsigned long long)(n_up_ok + n_up_rej), (unsigned long long)n_guard,
                (unsigned long long)n_drop2);
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        std::printf("Core_%u_coord_epochs_at_level", i);
        for (uint32_t l = 0; l < COORD_LEVELS; l++)
            std::printf(" %llu", (unsigned long long)level_epochs[i][l]);
        std::printf("\nCore_%u_coord_final_level %u\n", i, (unsigned)level[i]);
    }
    if (!vp.ways)
        return;
    std::printf("COORD_WSUM way_trials_kept=%llu way_trials=%llu unchanged=%llu gainer_throttles=%llu restores=%llu "
                "psr_grants=%llu polluters=%llu reclaimed=%llu ucp_kept=%llu coupled_kept=%llu coupled=%llu "
                "guard_rejects=%llu drop2=%llu\n",
                (unsigned long long)n_w_ok, (unsigned long long)(n_w_ok + n_w_rej), (unsigned long long)n_w_same,
                (unsigned long long)n_gainer, (unsigned long long)n_restore, (unsigned long long)n_grant,
                (unsigned long long)n_poll, (unsigned long long)n_reclaim, (unsigned long long)n_keepucp,
                (unsigned long long)n_coupled_ok, (unsigned long long)n_coupled, (unsigned long long)n_guard,
                (unsigned long long)n_drop2);
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        std::printf("Core_%u_coord_pf_accuracy %.3f\nCore_%u_coord_pf_timeliness %.3f\nCore_%u_coord_psr %.3f\n", i,
                    acc_ewma[i], i, tim_ewma[i], i, psr_of(i));
    uint64_t ep = 0;
    for (uint32_t l = 0; l < COORD_LEVELS; l++)
        ep += level_epochs[0][l];
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        std::printf("Core_%u_coord_avg_ways %.3f\nCore_%u_coord_final_ways %u\n", i,
                    ep ? (double)quota_epochs[i] / (double)ep : 0.0, i, cur_q[i]);
}
