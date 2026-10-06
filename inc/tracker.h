#ifndef INC_TRACKER_H_
#define INC_TRACKER_H_
#define window_switch_cycle_interval 1000000
#define window_width 4

extern long long int total_counts[LLC_SET][LLC_WAY];
extern unsigned long long int total_misses;
extern unsigned long long int total_writes;
//extern long long int write_count[LLC_WAY];
//extern int current_restricted_ways[4];
//extern long long int total_past_all_cores_cycles;

#endif
