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

bool coord_armed = false;

namespace {

enum Policy { POLICY_OFF = 0, POLICY_LOG = 1 };

Policy   policy       = POLICY_OFF;
uint64_t epoch_cycles = 1000000;
uint64_t next_epoch   = 0;
uint64_t epoch_index  = 0;

// cumulative counters of one private cache, as exposed by CACHE
struct PfCounters {
    uint64_t requested, issued, filled, useful, useless, late;
};

struct CoreSnap {
    uint64_t retired, cycles;
    PfCounters l1d, l2c;
    uint64_t llc_dacc, llc_dmiss;   // demand (LOAD + RFO) accesses / misses at the shared LLC, this core
    uint64_t llc_pfacc, llc_pfmiss; // prefetch accesses / misses at the shared LLC, this core
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
    } else {
        std::fprintf(stderr, "[COORD] unknown COORD_POLICY '%s' (use off|log)\n", p);
        std::exit(1);
    }
    if (const char *e = std::getenv("COORD_EPOCH_CYCLES")) {
        const long long n = std::atoll(e);
        if (n <= 0) {
            std::fprintf(stderr, "[COORD] COORD_EPOCH_CYCLES must be positive\n");
            std::exit(1);
        }
        epoch_cycles = (uint64_t)n;
    }
}

void coord_arm()
{
    if (policy == POLICY_OFF)
        return;
    take(last_snap);
    epoch_index = 0;
    next_epoch  = uncore.cycle + epoch_cycles;
    coord_armed = true;
    std::printf("[COORD] policy=log epoch_cycles=%llu cores=%u first_boundary=%llu\n",
                (unsigned long long)epoch_cycles, (unsigned)NUM_CPUS, (unsigned long long)next_epoch);
}

void coord_tick()
{
    if (uncore.cycle < next_epoch)
        return;
    epoch_boundary();
    next_epoch += epoch_cycles;
}
