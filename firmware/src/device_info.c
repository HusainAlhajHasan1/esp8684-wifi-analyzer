#include "device_info.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_app_desc.h"
#include "esp_chip_info.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_idf_version.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "version.h"

const char *device_reset_reason_str(void)
{
    switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "power_on";
    case ESP_RST_EXT:      return "external_pin";
    case ESP_RST_SW:       return "software";
    case ESP_RST_PANIC:    return "panic";
    case ESP_RST_INT_WDT:  return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT:      return "other_wdt";
    case ESP_RST_DEEPSLEEP:return "deep_sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO:     return "sdio";
    case ESP_RST_UNKNOWN:
    default:               return "unknown";
    }
}

cJSON *device_info_build(void)
{
    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return NULL;
    }

    esp_chip_info_t chip;
    esp_chip_info(&chip);

    cJSON_AddStringToObject(d, "model", DEVICE_MODEL);
    cJSON_AddStringToObject(d, "chip", DEVICE_CHIP);

    /* Chip revision is a real number on C2 (e.g. 0.2) — expose as string so the
     * app does not have to guess at formatting. */
    char rev[16];
    snprintf(rev, sizeof(rev), "v%d.%d", chip.revision / 100, chip.revision % 100);
    cJSON_AddStringToObject(d, "chip_rev", rev);
    cJSON_AddNumberToObject(d, "cores", chip.cores);

    uint32_t flash_size = 0;
    if (esp_flash_get_size(esp_flash_default_chip, &flash_size) != ESP_OK) {
        flash_size = 0;
    }
    cJSON_AddNumberToObject(d, "flash_bytes", (double)flash_size);

    uint8_t mac[6] = {0};
    if (esp_read_mac(mac, ESP_MAC_WIFI_STA) == ESP_OK) {
        char macstr[18];
        snprintf(macstr, sizeof(macstr), "%02X:%02X:%02X:%02X:%02X:%02X",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
        cJSON_AddStringToObject(d, "mac_sta", macstr);
    }

    const esp_app_desc_t *app = esp_app_get_description();
    cJSON *sw = cJSON_AddObjectToObject(d, "fw");
    if (sw != NULL) {
        /* FW_VERSION, not app->version: the ESP-IDF app descriptor defaults to
         * "1" and would otherwise contradict PROTOCOL.md and the Android app. */
        cJSON_AddStringToObject(sw, "version", FW_VERSION);
        cJSON_AddStringToObject(sw, "project", "esp8684-wifi-analyzer");
        cJSON_AddStringToObject(sw, "idf", esp_get_idf_version());
        cJSON_AddStringToObject(sw, "build_tag", FW_BUILD_TAG);
        cJSON_AddStringToObject(sw, "built", app->date);
        cJSON_AddStringToObject(sw, "time", app->time);
    }

    /* Built-for crystal frequency. A 26 MHz board running 40 MHz firmware
     * transmits everything 26/40 too slowly; reporting it here makes that
     * immediately visible instead of looking like a dead UART. */
    cJSON_AddNumberToObject(d, "xtal_mhz", CONFIG_XTAL_FREQ);

    cJSON_AddNumberToObject(d, "uptime_ms", (double)(esp_timer_get_time() / 1000));
    cJSON_AddNumberToObject(d, "heap_free", (double)esp_get_free_heap_size());
    cJSON_AddNumberToObject(d, "heap_min_free", (double)esp_get_minimum_free_heap_size());
    cJSON_AddStringToObject(d, "reset_reason", device_reset_reason_str());

    return d;
}
