#pragma once

#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"

/* Dispatches one inbound protocol line to its command handler.
 *
 * Runs in the UART RX task context: everything here must stay short and
 * non-blocking. Commands that touch the radio (Phase 2 scans) will enqueue work
 * on their own task and answer asynchronously — that is why responses carry the
 * request `id` rather than being ordered strictly one-per-request.
 */
void command_process_line(char *line, size_t len);

void command_processor_init(void);

/* Unsolicited heartbeat: {"type":"event.heartbeat"} every `period_ms`. */
void command_processor_heartbeat_start(uint32_t period_ms);
void command_processor_heartbeat_set(uint32_t period_ms);

/* Aggregate counters for the `stats` command. */
cJSON *command_processor_stats_build(void);
