#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"
#include "esp_err.h"
#include "protocol.h"

/* Persistent runtime configuration (NVS-backed).
 *
 * Only values that are safe to change at runtime are exposed. Anything that
 * would desynchronise the link (baud rate) is applied at next boot, and the
 * device says so explicitly in its ack. */

#define CFG_MAGIC     0x57464131u /* "WFA1" */
#define CFG_STRUCT_VER 1

typedef struct {
    uint32_t magic;
    uint16_t struct_ver;
    uint16_t reserved0;
    uint32_t scan_interval_ms; /* Phase 2 auto-scan period, 1000..60000 */
    uint32_t heartbeat_ms;     /* unsolicited heartbeat period, 1000..60000 */
    uint32_t baud;             /* 115200 | 230400 | 460800, applied on reboot */
    uint8_t  max_networks;     /* scan result cap, 1..32 (RAM bound, WFA_MAX_NETWORKS) */
    uint8_t  log_level;        /* 0 none, 1 error, 2 warn, 3 info, 4 debug */
    uint8_t  auto_scan;        /* reserved: continuous scan on boot (not enabled) */
    uint8_t  reserved1;
} app_config_t;

void                 app_config_init(void);
app_config_t        *app_config_get(void);

/* The line rate the UART is ACTUALLY running at this session (fixed at boot).
 * Differs from cfg->baud once the user saves a new baud, which is why
 * baud_pending exists. Never report cfg->baud as if it were live. */
uint32_t             app_config_running_baud(void);
esp_err_t            app_config_save(void);

/* Fills `dst` with the current config (caller keeps ownership of dst).
 * `baud_pending` is true when the saved baud differs from the running one. */
void                 app_config_to_json(cJSON *dst, uint32_t running_baud);

/* Applies validated fields from `params` to `out` (a copy), returning E_OK or
 * an error. Does not persist. */
proto_err_t          app_config_apply_json(const cJSON *params, app_config_t *out, bool *baud_changed);
