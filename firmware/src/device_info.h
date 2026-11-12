#pragma once

#include "cJSON.h"

/* Static, real-hardware facts about the attached board. No WiFi init required:
 * the STA MAC comes straight from eFuse, so `info` works before Phase 2's radio
 * bring-up. Caller owns the returned object. */
cJSON *device_info_build(void);

const char *device_reset_reason_str(void);
