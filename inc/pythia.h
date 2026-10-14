#ifndef PYTHIA_H
#define PYTHIA_H

#include <cstdint>
#include <vector>
#include <unordered_map>
#include <cstddef>

// Action space: set of prefetch offset deltas
const int PYTHIA_ACTIONS[] = {0, 1, -1, 2, -2, 3, -3, 4, -4, 6, -6, 8, -8, 12, -12, 16, -16};
const int NUM_ACTIONS = sizeof(PYTHIA_ACTIONS) / sizeof(PYTHIA_ACTIONS[0]);

// Feature Vector representing the State space
struct PythiaState {
    uint64_t pc;
    int64_t delta;
    uint8_t bw_level;

    bool operator==(const PythiaState& other) const {
        return pc == other.pc && delta == other.delta && bw_level == other.bw_level;
    }
};

namespace std {
    template <>
    struct hash<PythiaState> {
        size_t operator()(const PythiaState& s) const {
            return (std::hash<uint64_t>()(s.pc) ^ 
                   (std::hash<int64_t>()(s.delta) << 1) ^ 
                   (std::hash<uint8_t>()(s.bw_level) << 2));
        }
    };
}

// Entry in the Evaluation Queue to track outstanding prefetches and deliver RL rewards
struct EQEntry {
    uint64_t address;
    PythiaState state;
    int action_idx;
    uint64_t issue_cycle;
    bool hit_by_demand;
};

// Global Event Callback Hooks used in cache.cc
void pythia_notify_demand_hit(uint32_t cpu, uint64_t addr);
void pythia_notify_late_prefetch(uint32_t cpu, uint64_t addr);

#endif // PYTHIA_H