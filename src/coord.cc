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

enum Policy { POLICY_OFF = 0, POLICY_LOG = 1, POLICY_STATIC = 2 };

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

void epoch_boundary()
{
    Snap now;
    take(now);

    // one global line, then one line per core; all values are deltas over the epoch that just ended
    std::printf("COORD_G e=%llu cycle=%llu dram_bw=%u dram_enq=%llu\n",
                (unsigned long long)epoch_index, (unsigned long long)uncore.cycle,
                (unsigned)uncore.DRAM.bw, (unsigned long long)(now.dram_enqueue - last_snap.dram_enqueue));

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
    } else {
        std::fprintf(stderr, "[COORD] unknown COORD_POLICY '%s' (use off|log|static)\n", p);
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
                policy == POLICY_STATIC ? "static" : "log", (unsigned long long)epoch_cycles, (unsigned)NUM_CPUS, (unsigned long long)next_epoch);
}

void coord_tick()
{
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
