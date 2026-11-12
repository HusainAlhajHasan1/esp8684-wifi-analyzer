#include "wifi_scanner.h"

#include <string.h>

#include "app_config.h"
#include "cJSON.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "network_classifier.h"
#include "protocol.h"

static const char *TAG = "scan";

/* One page must stay comfortably under any host buffer, including the USB
 * bridge's. PROTOCOL.md §7 fixes this at ~1400 bytes; we aim lower. */
#define PAGE_BUDGET 1300
#define SCAN_TASK_STACK 5120
#define SCAN_TASK_PRIO 5
#define SCAN_MIN_INTERVAL_MS 1000
#define SCAN_MAX_INTERVAL_MS 60000

/* Active scan timings per channel. 90/120 ms is a deliberate compromise:
 * shorter misses weak APs, longer makes a 13-channel sweep feel slow. */
#define SCAN_TIME_MIN_MS 90
#define SCAN_TIME_MAX_MS 120

static wifi_ap_record_t s_records[WFA_MAX_NETWORKS];
static uint16_t s_count;
static uint32_t s_scan_id;
static uint32_t s_last_scan_ms;
static uint32_t s_interval_ms = 5000;
static volatile bool s_scanning;
static volatile bool s_continuous;
static volatile bool s_stop_requested;
static volatile bool s_event_pending;
static int64_t s_scan_started_us;
static int64_t s_last_scan_end_us;
static uint16_t s_dropped;
static TaskHandle_t s_task;

static struct {
    uint32_t scans;
    uint32_t failed;
    uint32_t stopped;
} s_stats;

static void emit_pages(uint32_t scan_id, bool cached);

/* ---------------------------------------------------------------- event side */
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
        /* Do nothing heavy here: the Wi-Fi event task has a small stack and
         * other events must not wait behind JSON serialisation. */
        s_event_pending = true;
        if (s_task != NULL) {
            xTaskNotifyGive(s_task);
        }
    }
}

/* ------------------------------------------------------------- result read */
static void handle_scan_done(void)
{
    int64_t now_us = esp_timer_get_time();
    s_last_scan_ms = (uint32_t)((now_us - s_scan_started_us) / 1000);
    s_last_scan_end_us = now_us;

    if (s_stop_requested) {
        /* Scan was cancelled: free the driver's result buffer without emitting
         * a partial list that a client might mistake for a full sweep. */
        esp_wifi_clear_ap_list();
        s_stop_requested = false;
        s_scanning = false;
        s_stats.stopped++;
        return;
    }

    uint16_t seen = 0;
    esp_err_t err = esp_wifi_scan_get_ap_num(&seen);
    if (err != ESP_OK) {
        s_scanning = false;
        s_stats.failed++;
        proto_emit_error(0, E_INTERNAL, "scan result count failed: %s", esp_err_to_name(err));
        return;
    }

    uint16_t want = seen > WFA_MAX_NETWORKS ? WFA_MAX_NETWORKS : seen;
    s_dropped = (uint16_t)(seen - want);

    if (want > 0) {
        err = esp_wifi_scan_get_ap_records(&want, s_records);
        if (err != ESP_OK) {
            s_scanning = false;
            s_stats.failed++;
            esp_wifi_clear_ap_list();
            proto_emit_error(0, E_INTERNAL, "scan result read failed: %s", esp_err_to_name(err));
            return;
        }
    } else {
        esp_wifi_clear_ap_list(); /* frees the driver buffer for an empty sweep */
    }

    s_count = want;
    s_scanning = false;
    s_stats.scans++;

    if (s_dropped > 0) {
        ESP_LOGW(TAG, "%u networks seen, %u kept (limit %d)", seen, want, WFA_MAX_NETWORKS);
    }
    emit_pages(s_scan_id, false);
}

/* -------------------------------------------------------------- emit pages */
static void emit_pages(uint32_t scan_id, bool cached)
{
    /* Memory strategy, on purpose:
     *   pass 1 renders ONE network object at a time to a stack buffer just to
     *          measure it, then frees it. Peak heap here is a single object.
     *   pass 2 rebuilds only the objects belonging to the page being emitted.
     *
     * The earlier version kept all 32 objects alive AND duplicated them into
     * each page - roughly 70 KB of transient heap on a board with ~72 KB free
     * after Wi-Fi init. It would have failed only in a crowded environment,
     * which is exactly where an analyzer is needed. */
    uint16_t lens[WFA_MAX_NETWORKS];
    char probe_buf[1024];

    for (uint16_t i = 0; i < s_count; i++) {
        net_class_t cls;
        net_classify(&s_records[i], &cls);
        cJSON *o = net_to_json(&s_records[i], &cls);
        if (o == NULL) {
            proto_emit_error(0, E_NO_MEM, "out of memory measuring scan result");
            return;
        }
        lens[i] = cJSON_PrintPreallocated(o, probe_buf, sizeof(probe_buf), false)
                      ? (uint16_t)(strlen(probe_buf) > 0xFFFF ? 0xFFFF : strlen(probe_buf))
                      : (uint16_t)sizeof(probe_buf);
        cJSON_Delete(o);
    }

    int pages = 1;
    size_t acc = 0;
    uint8_t page_of[WFA_MAX_NETWORKS];
    for (uint16_t i = 0; i < s_count; i++) {
        size_t need = (size_t)lens[i] + (acc > 0 ? 1 : 0);
        if (acc > 0 && acc + need > PAGE_BUDGET) {
            pages++;
            acc = 0;
        }
        page_of[i] = (uint8_t)(pages - 1);
        acc += (size_t)lens[i] + 1;
    }

    for (int p = 0; p < pages; p++) {
        cJSON *d = cJSON_CreateObject();
        if (d == NULL) {
            return;
        }
        cJSON_AddNumberToObject(d, "scan_id", scan_id);
        cJSON_AddNumberToObject(d, "page", p + 1);
        cJSON_AddNumberToObject(d, "pages", pages);
        cJSON_AddNumberToObject(d, "total", s_count);
        cJSON_AddBoolToObject(d, "truncated", s_dropped > 0);
        cJSON_AddNumberToObject(d, "scan_ms", s_last_scan_ms);
        if (s_dropped > 0) {
            cJSON_AddNumberToObject(d, "dropped", s_dropped);
        }
        if (cached) {
            cJSON_AddBoolToObject(d, "cached", true);
        }

        cJSON *arr = cJSON_AddArrayToObject(d, "networks");
        if (arr == NULL) {
            cJSON_Delete(d);
            return;
        }
        for (uint16_t i = 0; i < s_count; i++) {
            if (page_of[i] != (uint8_t)p) {
                continue;
            }
            net_class_t cls;
            net_classify(&s_records[i], &cls);
            cJSON *o = net_to_json(&s_records[i], &cls);
            if (o != NULL) {
                cJSON_AddItemToArray(arr, o);
            }
        }
        proto_emit("scan_result", 0, d); /* unsolicited: id 0 per contract */
    }
}

/* ------------------------------------------------------------------- task */
static void scanner_task(void *arg)
{
    (void)arg;
    for (;;) {
        uint32_t signalled = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(250));
        if (signalled > 0 && s_event_pending) {
            s_event_pending = false;
            handle_scan_done();
        }
        if (s_continuous && !s_scanning && !s_stop_requested) {
            int64_t since = esp_timer_get_time() - s_last_scan_end_us;
            if (since >= (int64_t)s_interval_ms * 1000) {
                wifi_scan_config_t cfg = {
                    .ssid = NULL,
                    .bssid = NULL,
                    .channel = 0,
                    .show_hidden = true,
                    .scan_type = WIFI_SCAN_TYPE_ACTIVE,
                };
                cfg.scan_time.active.min = SCAN_TIME_MIN_MS;
                cfg.scan_time.active.max = SCAN_TIME_MAX_MS;
                s_scan_started_us = esp_timer_get_time();
                if (esp_wifi_scan_start(&cfg, false) == ESP_OK) {
                    s_scan_id++;
                    s_scanning = true;
                } else {
                    s_stats.failed++;
                }
            }
        }
    }
}

/* ------------------------------------------------------------------- API */
esp_err_t wifi_scanner_init(void)
{
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    if (esp_netif_create_default_wifi_sta() == NULL) {
        return ESP_FAIL;
    }

    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&ic);
    if (err != ESP_OK) {
        return err;
    }

    /* Scan results are transient. Keeping Wi-Fi config out of NVS means a scan
     * every few seconds can never wear the flash partition. */
    esp_wifi_set_storage(WIFI_STORAGE_RAM);

    err = esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_SCAN_DONE, &wifi_event_handler, NULL);
    if (err != ESP_OK) {
        return err;
    }

    err = esp_wifi_set_mode(WIFI_MODE_STA); /* STA only: never connects */
    if (err != ESP_OK) {
        return err;
    }
    err = esp_wifi_start();
    if (err != ESP_OK) {
        return err;
    }

    /* Modem sleep makes scan results erratic and delays beacons; a network
     * analyzer should not be in a power-saving state while measuring. */
    esp_wifi_set_ps(WIFI_PS_NONE);

    if (xTaskCreate(scanner_task, "wifi_scan", SCAN_TASK_STACK, NULL,
                    SCAN_TASK_PRIO, &s_task) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    ESP_LOGI(TAG, "scanner ready (max %d networks, page budget %d B)",
             WFA_MAX_NETWORKS, PAGE_BUDGET);
    return ESP_OK;
}

esp_err_t wifi_scanner_start(bool continuous, uint32_t interval_ms, uint32_t *out_scan_id)
{
    if (s_task == NULL) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_scanning) {
        return ESP_ERR_INVALID_STATE; /* busy: caller maps this to E_BUSY */
    }

    if (interval_ms < SCAN_MIN_INTERVAL_MS) {
        interval_ms = SCAN_MIN_INTERVAL_MS;
    } else if (interval_ms > SCAN_MAX_INTERVAL_MS) {
        interval_ms = SCAN_MAX_INTERVAL_MS;
    }
    s_interval_ms = interval_ms;
    s_continuous = continuous;
    s_stop_requested = false;

    wifi_scan_config_t cfg = {
        .ssid = NULL,
        .bssid = NULL,
        .channel = 0,
        .show_hidden = true,
        .scan_type = WIFI_SCAN_TYPE_ACTIVE,
    };
    cfg.scan_time.active.min = SCAN_TIME_MIN_MS;
    cfg.scan_time.active.max = SCAN_TIME_MAX_MS;

    s_scan_started_us = esp_timer_get_time();
    esp_err_t err = esp_wifi_scan_start(&cfg, false);
    if (err != ESP_OK) {
        s_stats.failed++;
        return err;
    }
    s_scan_id++;
    s_scanning = true;
    if (out_scan_id != NULL) {
        *out_scan_id = s_scan_id;
    }
    return ESP_OK;
}

esp_err_t wifi_scanner_stop(void)
{
    s_continuous = false;
    if (!s_scanning) {
        return ESP_OK;
    }
    s_stop_requested = true;
    esp_err_t err = esp_wifi_scan_stop();
    if (err == ESP_ERR_WIFI_NOT_STARTED || err == ESP_ERR_WIFI_STATE) {
        return ESP_OK; /* already finished; the event will clean up */
    }
    return err;
}

bool wifi_scanner_emit_cached(void)
{
    if (s_stats.scans == 0) {
        return false;
    }
    emit_pages(s_scan_id, true);
    return true;
}

bool wifi_scanner_is_scanning(void)
{
    return s_scanning;
}

uint32_t wifi_scanner_last_scan_id(void)
{
    return s_scan_id;
}

void wifi_scanner_status_json(cJSON *dst)
{
    if (dst == NULL) {
        return;
    }
    cJSON_AddNumberToObject(dst, "scan_id", s_scan_id);
    cJSON_AddBoolToObject(dst, "scanning", s_scanning);
    cJSON_AddBoolToObject(dst, "continuous", s_continuous);
    cJSON_AddNumberToObject(dst, "interval_ms", s_interval_ms);
    cJSON_AddNumberToObject(dst, "networks", s_count);
    cJSON_AddNumberToObject(dst, "last_scan_ms", s_last_scan_ms);
    cJSON_AddNumberToObject(dst, "scans", s_stats.scans);
    cJSON_AddNumberToObject(dst, "failed", s_stats.failed);
    cJSON_AddNumberToObject(dst, "dropped", s_dropped);
    cJSON_AddNumberToObject(dst, "max_networks", WFA_MAX_NETWORKS);
}
