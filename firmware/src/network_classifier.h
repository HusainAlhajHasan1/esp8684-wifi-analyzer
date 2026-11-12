#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "cJSON.h"
#include "esp_wifi_types.h"

/* Classification is deliberately a pure function of what the radio actually
 * reported. Nothing here guesses: if the driver did not measure it, it is
 * either absent from the JSON or explicitly "unknown". */

typedef struct {
    const char *security;   /* one of the strings in PROTOCOL.md §7 */
    int quality;            /* 0..100, 2*(rssi+100) clamped */
    int freq_mhz;           /* 2407 + 5*channel, channel 14 = 2484 */
    int width_mhz;          /* 20 or 40, from the secondary channel */
    bool hidden;            /* SSID length 0 - BSSID/channel/RSSI only */
    bool wps;               /* WPS advertised in capability info */
    bool legacy;            /* WEP or 802.11b-only */
    bool strong;            /* WPA3/OWE, i.e. a modern security mode */
    bool weak;              /* open OR WPS advertised - a config observation */
} net_class_t;

/* Fills `out` from a scan record. Never invents an SSID. */
void net_classify(const wifi_ap_record_t *rec, net_class_t *out);

/* Builds the frozen network object from PROTOCOL.md §7. Caller owns the result.
 * Returns NULL only if allocation fails. */
cJSON *net_to_json(const wifi_ap_record_t *rec, const net_class_t *cls);

/* True when `len` bytes are valid UTF-8. Used to decide whether the SSID can be
 * carried as a JSON string at all; when it cannot, the raw bytes go out as
 * ssid_hex and the string field is omitted rather than mangled. */
bool net_utf8_valid(const uint8_t *s, size_t len);
