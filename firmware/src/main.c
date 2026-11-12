#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "cJSON.h"
#include "esp_idf_version.h"
#include "esp_rom_sys.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"

#include "app_config.h"
#include "command_processor.h"
#include "device_info.h"
#include "log_router.h"
#include "protocol.h"
#include "serial_manager.h"
#include "wifi_scanner.h"
#include "version.h"

static const char *TAG = "main";

/* ---------------------------------------------------------------------------
 * Diagnostic breadcrumbs (WFA_DIAG_BUILD only).
 *
 * esp_rom_printf writes straight to the UART FIFO with no driver, no logs, no
 * allocation - it works even if everything above it is broken. That makes it
 * the right tool for "the board is silent, where exactly does it die?".
 * Compiled to nothing in normal builds, so the release image stays clean.
 * ------------------------------------------------------------------------- */
#ifdef WFA_DIAG_BUILD
#define DIAG(fmt, ...) esp_rom_printf("[WFA-DIAG] " fmt "\r\n", ##__VA_ARGS__)
#else
#define DIAG(fmt, ...) do { } while (0)
#endif

static esp_log_level_t level_from_int(uint8_t lvl)
{
    switch (lvl) {
    case 0:  return ESP_LOG_NONE;
    case 1:  return ESP_LOG_ERROR;
    case 2:  return ESP_LOG_WARN;
    case 3:  return ESP_LOG_INFO;
    case 4:
    default: return ESP_LOG_DEBUG;
    }
}

static void wdt_setup(void)
{
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
    esp_task_wdt_config_t cfg = {
        .timeout_ms = 15000,
        .idle_core_mask = 0, /* do not watch the idle task (single core, busy loops) */
        .trigger_panic = true,
    };
    esp_err_t err = esp_task_wdt_init(&cfg);
    if (err == ESP_ERR_INVALID_STATE) {
        /* Already initialised by sdkconfig defaults on some IDF revisions. */
        esp_task_wdt_reconfigure(&cfg);
    }
    if (esp_task_wdt_add(NULL) != ESP_OK) {
        ESP_LOGW(TAG, "could not subscribe main task to wdt");
    }
#else
    esp_task_wdt_init(15, true);
    esp_task_wdt_add(NULL);
#endif
}

void app_main(void)
{
    DIAG("app_main reached (diag build)");

    /* 1. Funnel all IDF logs into JSON before anything else can print. */
#ifdef WFA_DIAG_BUILD
    /* In the diagnostic build we deliberately SKIP the JSON log router so the
     * normal ESP-IDF boot log stays human-readable. If you see coloured
     * "I (123) ..." text from the diag firmware, the app IS running. */
    esp_log_level_set("*", ESP_LOG_INFO);
#else
    log_router_init();
#endif

    /* 2. Persistent config (NVS) — also gives us the line rate to open UART0. */
    app_config_init();
    app_config_t *cfg = app_config_get();
    esp_log_level_set("*", level_from_int(cfg->log_level));

    /* 3. USB-serial side: UART0 via the board's bridge. */
    serial_cfg_t scfg = {
        .baud   = cfg->baud,
        .tx_pin = PIN_UART_TX,
        .rx_pin = PIN_UART_RX,
    };
    DIAG("config ok: baud=%u log_level=%u heartbeat=%u", (unsigned)cfg->baud,
         (unsigned)cfg->log_level, (unsigned)cfg->heartbeat_ms);

    esp_err_t err = serial_init(&scfg);
    if (err != ESP_OK) {
        /* Without the link the device is useless to the host; reset and retry
         * rather than sit in a broken state. UART init failure is essentially
         * always a pin/config error, so this will loop — visible in the log. */
        ESP_LOGE(TAG, "uart0 init failed: %s", esp_err_to_name(err));
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_restart();
    }

    DIAG("uart0 ok: %u baud, tx=%d rx=%d", (unsigned)scfg.baud, scfg.tx_pin, scfg.rx_pin);

    /* 4. Command dispatch + watchdog + heartbeat. */
    command_processor_init();
    serial_set_line_handler(command_process_line);
    wdt_setup();
    command_processor_heartbeat_start(cfg->heartbeat_ms);

    /* 4b. Radio. A Wi-Fi init failure must NOT kill the link: the host can
     * still read config and logs, and scan.* will answer E_INTERNAL instead of
     * the board going dark. Stability over features. */
    esp_err_t werr = wifi_scanner_init();
    if (werr != ESP_OK) {
        ESP_LOGE(TAG, "wifi init failed: %s (scan.* will return E_INTERNAL)",
                 esp_err_to_name(werr));
    } else {
        ESP_LOGI(TAG, "wifi scanner ready");
    }

    DIAG("emitting event.boot now");

    /* 5. Announce ourselves. The host uses this frame as the handshake. */
    cJSON *info = device_info_build();
    if (info != NULL) {
        cJSON_AddStringToObject(info, "ready", "phase2");
        proto_emit_event("event.boot", info);
    }

    ESP_LOGI(TAG, "firmware %s (%s) running, proto v%d", FW_VERSION, FW_BUILD_TAG,
             PROTO_VERSION);

    DIAG("event.boot emitted, entering main loop");

    for (;;) {
        esp_task_wdt_reset();
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
