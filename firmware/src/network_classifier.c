#include "network_classifier.h"

#include <stdio.h>
#include <string.h>

#include "esp_wifi.h"

#define CLAMP(v, lo, hi) ((v) < (lo) ? (lo) : ((v) > (hi) ? (hi) : (v)))

bool net_utf8_valid(const uint8_t *s, size_t len)
{
    size_t i = 0;
    while (i < len) {
        uint8_t c = s[i];
        size_t n;
        if (c < 0x80) {
            i++;
            continue;
        } else if ((c & 0xE0) == 0xC0) {
            n = 1;
        } else if ((c & 0xF0) == 0xE0) {
            n = 2;
        } else if ((c & 0xF8) == 0xF0) {
            n = 3;
        } else {
            return false; /* stray continuation or invalid lead byte */
        }
        if (i + n >= len) {
            return false;
        }
        for (size_t k = 1; k <= n; k++) {
            if ((s[i + k] & 0xC0) != 0x80) {
                return false;
            }
        }
        i += n + 1;
    }
    return true;
}

static const char *security_str(wifi_auth_mode_t mode)
{
    switch (mode) {
    case WIFI_AUTH_OPEN:
        return "open";
    case WIFI_AUTH_WEP:
        return "wep";
    case WIFI_AUTH_WPA_PSK:
        return "wpa";
    case WIFI_AUTH_WPA2_PSK:
        return "wpa2";
    case WIFI_AUTH_WPA_WPA2_PSK:
        return "wpa/wpa2";
    case WIFI_AUTH_WPA2_ENTERPRISE: /* same enum value as WIFI_AUTH_ENTERPRISE */
        return "wpa2-enterprise";
    case WIFI_AUTH_WPA3_PSK:
        return "wpa3";
    case WIFI_AUTH_WPA2_WPA3_PSK:
        return "wpa2/wpa3";
    case WIFI_AUTH_WAPI_PSK:
        return "wapi";
    case WIFI_AUTH_OWE:
        return "owe";
    case WIFI_AUTH_WPA3_ENT_192:
        return "wpa3-192";
    case WIFI_AUTH_WPA3_EXT_PSK:
    case WIFI_AUTH_WPA3_EXT_PSK_MIXED_MODE:
        return "wpa3";
    case WIFI_AUTH_WPA3_ENTERPRISE:
        return "wpa3-enterprise";
    case WIFI_AUTH_WPA2_WPA3_ENTERPRISE:
        return "wpa2/wpa3-enterprise";
    case WIFI_AUTH_WPA_ENTERPRISE:
        return "wpa-enterprise";
    default:
        return "unknown";
    }
}

void net_classify(const wifi_ap_record_t *rec, net_class_t *out)
{
    if (rec == NULL || out == NULL) {
        return;
    }
    memset(out, 0, sizeof(*out));

    out->security = security_str(rec->authmode);
    out->quality = CLAMP(2 * ((int)rec->rssi + 100), 0, 100);

    if (rec->primary == 14) {
        out->freq_mhz = 2484; /* Japan-only channel, not 2407 + 5*14 */
    } else if (rec->primary >= 1 && rec->primary <= 13) {
        out->freq_mhz = 2407 + 5 * (int)rec->primary;
    } else {
        out->freq_mhz = 0; /* driver reported something impossible: say so */
    }

    out->width_mhz = (rec->second == WIFI_SECOND_CHAN_NONE) ? 20 : 40;
    out->hidden = (rec->ssid[0] == 0);
    out->wps = (rec->wps != 0);

    bool b_only = (rec->phy_11b != 0) && (rec->phy_11g == 0) && (rec->phy_11n == 0);
    out->legacy = (rec->authmode == WIFI_AUTH_WEP) || b_only;

    bool modern = (rec->authmode == WIFI_AUTH_WPA3_PSK) ||
                  (rec->authmode == WIFI_AUTH_WPA2_WPA3_PSK) ||
                  (rec->authmode == WIFI_AUTH_WPA3_ENT_192) ||
                  (rec->authmode == WIFI_AUTH_WPA3_EXT_PSK) ||
                  (rec->authmode == WIFI_AUTH_WPA3_EXT_PSK_MIXED_MODE) ||
                  (rec->authmode == WIFI_AUTH_OWE);
    out->strong = modern;

    /* "weak" describes configuration only: open, or WPS advertised. It is not a
     * claim that the network can be broken - the UI must say "potentially weak". */
    out->weak = (rec->authmode == WIFI_AUTH_OPEN) || out->wps;
}

cJSON *net_to_json(const wifi_ap_record_t *rec, const net_class_t *cls)
{
    if (rec == NULL || cls == NULL) {
        return NULL;
    }
    cJSON *o = cJSON_CreateObject();
    if (o == NULL) {
        return NULL;
    }

    /* SSID: length is implicit (uint8_t[33], NUL terminated by the driver).
     * A hidden network has a zero length SSID - we emit "" and hidden:true and
     * never substitute a placeholder name. */
    size_t ssid_len = strnlen((const char *)rec->ssid, sizeof(rec->ssid));
    if (ssid_len == 0) {
        cJSON_AddStringToObject(o, "ssid", "");
    } else if (net_utf8_valid(rec->ssid, ssid_len)) {
        cJSON_AddStringToObject(o, "ssid", (const char *)rec->ssid);
    } else {
        /* Not representable as UTF-8: send the raw bytes as hex so the client
         * can still render something truthful, and omit the string entirely. */
        char hex[2 * 32 + 1];
        static const char *H = "0123456789abcdef";
        size_t n = ssid_len > 32 ? 32 : ssid_len;
        for (size_t i = 0; i < n; i++) {
            hex[i * 2] = H[(rec->ssid[i] >> 4) & 0xF];
            hex[i * 2 + 1] = H[rec->ssid[i] & 0xF];
        }
        hex[n * 2] = '\0';
        cJSON_AddStringToObject(o, "ssid_hex", hex);
    }

    char bssid[18];
    snprintf(bssid, sizeof(bssid), "%02X:%02X:%02X:%02X:%02X:%02X",
             rec->bssid[0], rec->bssid[1], rec->bssid[2],
             rec->bssid[3], rec->bssid[4], rec->bssid[5]);
    cJSON_AddStringToObject(o, "bssid", bssid);

    cJSON_AddNumberToObject(o, "rssi", rec->rssi);
    cJSON_AddNumberToObject(o, "quality", cls->quality);
    cJSON_AddNumberToObject(o, "channel", rec->primary);
    if (cls->freq_mhz > 0) {
        cJSON_AddNumberToObject(o, "freq_mhz", cls->freq_mhz);
    }
    cJSON_AddStringToObject(o, "band", "2.4ghz");
    cJSON_AddNumberToObject(o, "width_mhz", cls->width_mhz);
    cJSON_AddStringToObject(o, "security", cls->security);
    cJSON_AddBoolToObject(o, "hidden", cls->hidden);
    cJSON_AddBoolToObject(o, "wps", cls->wps);

    cJSON *phy = cJSON_AddArrayToObject(o, "phy");
    if (phy != NULL) {
        if (rec->phy_11b) {
            cJSON_AddItemToArray(phy, cJSON_CreateString("b"));
        }
        if (rec->phy_11g) {
            cJSON_AddItemToArray(phy, cJSON_CreateString("g"));
        }
        if (rec->phy_11n) {
            cJSON_AddItemToArray(phy, cJSON_CreateString("n"));
        }
    }

    cJSON *flags = cJSON_AddArrayToObject(o, "flags");
    if (flags != NULL) {
        if (cls->weak) {
            cJSON_AddItemToArray(flags, cJSON_CreateString("weak"));
        }
        if (cls->wps) {
            cJSON_AddItemToArray(flags, cJSON_CreateString("wps"));
        }
        if (cls->hidden) {
            cJSON_AddItemToArray(flags, cJSON_CreateString("hidden"));
        }
        if (cls->legacy) {
            cJSON_AddItemToArray(flags, cJSON_CreateString("legacy"));
        }
        if (cls->strong) {
            cJSON_AddItemToArray(flags, cJSON_CreateString("strong"));
        }
        if (rec->authmode == WIFI_AUTH_OPEN) {
            cJSON_AddItemToArray(flags, cJSON_CreateString("open"));
        }
    }
    return o;
}
