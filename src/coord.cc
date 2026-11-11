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
    uint32_t trial = 2, cooldown = 16, calm = 4, retest = 32;
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
    trial_core = j;
    trial_dir = dir;
    trial_left = vp.trial;
    trial_n = 0;
    trial_score = trial_self = 0.0;
    update_gate();
    std::printf("COORD_V e=%llu trial core=%d %s to level %u (%s, bw=%.2f, useless=%.3f, traffic=%.3f, acc=%.3f)\n",
                (unsigned long long)epoch_index, j, dir < 0 ? "down" : "up", (unsigned)level[j], why, bwavg,
                cost_ewma[j], traf_ewma[j], acc_ewma[j]);
}

// One decision step at the end of an epoch (deltas between o and n).
void victim_step(const Snap &n, const Snap &o, double bwavg)
{
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
        }
        level_epochs[i][level[i]]++;
    }
    calm_epochs = bwavg >= vp.bw_hi ? 0 : calm_epochs + 1;
    if (!ref_ok) {
        for (uint32_t i = 0; i < NUM_CPUS; i++)
            ref_ipc[i] = ipc[i];
        ref_ok = true;
        return;
    }
    if (in_trial) {
        double inv = 0.0;
        for (uint32_t i = 0; i < NUM_CPUS; i++) {
            const double r = ref_ipc[i] > 0 ? ipc[i] / ref_ipc[i] : 1.0;
            inv += 1.0 / (r > 1e-3 ? r : 1e-3);
        }
        trial_score += (double)NUM_CPUS / inv;                // harmonic mean: throughput that respects fairness
        trial_self += ref_ipc[trial_core] > 0 ? ipc[trial_core] / ref_ipc[trial_core] : 1.0;
        trial_n++;
        if (--trial_left > 0)
            return;
        const double score = trial_score / trial_n, self = trial_self / trial_n;
        const bool ok = trial_dir < 0 ? score >= 1.0 + vp.gain : score >= 1.0 - vp.gain / 2;
        const int j = trial_core;
        if (ok) {
            (trial_dir < 0 ? n_down_ok : n_up_ok)++;
            cooldown_left[j] = trial_dir < 0 ? vp.cooldown / 4 : 0;
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
    if (bwavg >= vp.bw_hi) {
        // Pressure: two kinds of suspects, tried in turn -- (a) self-harm: most USELESS prefetch DRAM traffic
        // (inaccurate prefetchers, e.g. IPCP on irregular cache-sensitive code); (b) neighbour harm: most prefetch
        // DRAM traffic of any kind (accurate streamers that eat the bandwidth the partition-protected cores need).
        // The trial itself decides whether throttling helps the system.
        for (uint32_t k = 0; k < 2; k++) {
            const double *m = ((rank_turn + k) % 2 == 0) ? cost_ewma : traf_ewma;
            double total = 0.0;
            int j = -1;
            for (uint32_t i = 0; i < NUM_CPUS; i++) {
                total += m[i];
                if (level[i] > 0 && !cooldown_left[i] && (j < 0 || m[i] > m[j]))
                    j = (int)i;
            }
            if (j >= 0 && total > 0 && m[j] >= vp.share * total / NUM_CPUS) {
                const bool useless = m == cost_ewma;
                rank_turn++;
                start_trial(j, -1, useless ? "pressure/useless-traffic" : "pressure/bandwidth-hog", bwavg);
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
    for (uint32_t i = 0; i < NUM_CPUS; i++)
        level[i] = COORD_LEVELS - 1;
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
        cnt("COORD_V_TRIAL", vp.trial);
        cnt("COORD_V_COOLDOWN", vp.cooldown);
        cnt("COORD_V_CALM", vp.calm);
        cnt("COORD_V_RETEST", vp.retest);
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
    take(last_snap);
    epoch_index = 0;
    next_epoch  = uncore.cycle + epoch_cycles;
    coord_armed = true;
    std::printf("[COORD] policy=%s epoch_cycles=%llu cores=%u first_boundary=%llu\n",
                policy == POLICY_STATIC ? "static" : policy == POLICY_VICTIM ? "victim" : "log",
                (unsigned long long)epoch_cycles, (unsigned)NUM_CPUS, (unsigned long long)next_epoch);
    if (policy == POLICY_VICTIM)
        std::printf("[COORD] victim: bw>=%.2f gain=%.4f share=%.2f trial=%u cooldown=%u calm=%u retest=%u\n",
                    vp.bw_hi, vp.gain, vp.share, vp.trial, vp.cooldown, vp.calm, vp.retest);
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
    std::printf("COORD_VSUM trials_down_kept=%llu trials_down=%llu trials_up_kept=%llu trials_up=%llu\n",
                (unsigned long long)n_down_ok, (unsigned long long)(n_down_ok + n_down_rej),
                (unsigned long long)n_up_ok, (unsigned long long)(n_up_ok + n_up_rej));
    for (uint32_t i = 0; i < NUM_CPUS; i++) {
        std::printf("Core_%u_coord_epochs_at_level", i);
        for (uint32_t l = 0; l < COORD_LEVELS; l++)
            std::printf(" %llu", (unsigned long long)level_epochs[i][l]);
        std::printf("\nCore_%u_coord_final_level %u\n", i, (unsigned)level[i]);
    }
}
