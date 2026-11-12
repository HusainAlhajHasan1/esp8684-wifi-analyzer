#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "cJSON.h"
#include "esp_err.h"

/* Hard ceiling on networks held per scan. 32 * sizeof(wifi_ap_record_t) is a
 * static ~3.6 KB, which is the RAM-vs-usefulness compromise for ESP8684.
 * If the radio sees more, the excess is reported honestly via "dropped" and
 * "truncated" rather than silently ignored. */
#define WFA_MAX_NETWORKS 32

/* Brings up esp_netif + Wi-Fi in STA mode (no credentials, never connects) and
 * starts the scanner task. Scanning is the only thing this build does with the
 * radio. Returns ESP_OK on success. */
esp_err_t wifi_scanner_init(void);

/* Starts one scan, or a repeating one when `continuous` is true.
 * `interval_ms` is clamped to 1000..60000 and only used when continuous.
 * Acknowledges immediately: results arrive later as unsolicited scan_result
 * frames (see PROTOCOL.md §7). */
esp_err_t wifi_scanner_start(bool continuous, uint32_t interval_ms, uint32_t *out_scan_id);

/* Cancels continuous scanning and aborts an in-flight scan. */
esp_err_t wifi_scanner_stop(void);

/* Re-emits the cached result set (same scan_id, cached:true) so a client that
 * missed a page can ask again without triggering RF activity. Returns false
 * when no scan has completed yet. */
bool wifi_scanner_emit_cached(void);

bool wifi_scanner_is_scanning(void);
uint32_t wifi_scanner_last_scan_id(void);

/* Status object for `scan.get`: {scan_id, scanning, continuous, interval_ms,
 * networks, last_scan_ms, scans, failed, dropped} */
void wifi_scanner_status_json(cJSON *dst);
