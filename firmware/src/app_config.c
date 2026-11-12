#include "app_config.h"

#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "version.h"

static const char *TAG = "config";

#define NVS_NS  "wfa"
#define NVS_KEY "cfg"

static app_config_t s_cfg;
static uint32_t     s_running_baud = DEFAULT_BAUD;

static bool baud_is_valid(uint32_t b)
{
    return (b == 115200u) || (b == 230400u) || (b == 460800u);
}

static uint32_t clamp_u32(uint32_t v, uint32_t lo, uint32_t hi)
{
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

static void set_defaults(app_config_t *c)
{
    memset(c, 0, sizeof(*c));
    c->magic            = CFG_MAGIC;
    c->struct_ver       = CFG_STRUCT_VER;
    c->scan_interval_ms = 5000;
    c->heartbeat_ms     = 5000;
    c->baud             = DEFAULT_BAUD;
    c->max_networks     = 32;
    c->log_level        = 2; /* WARN — keeps the link quiet by default */
    c->auto_scan        = 0;
}

/* Clamp anything that came from flash into a safe range. A corrupt NVS blob is
 * treated as "keep defaults for the bad field", never as a fatal error. */
static void sanitize(app_config_t *c)
{
    if (c->magic != CFG_MAGIC || c->struct_ver != CFG_STRUCT_VER) {
        set_defaults(c);
        return;
    }
    c->scan_interval_ms = clamp_u32(c->scan_interval_ms, 1000, 60000);
    c->heartbeat_ms     = clamp_u32(c->heartbeat_ms, 1000, 60000);
    if (!baud_is_valid(c->baud)) {
        c->baud = DEFAULT_BAUD;
    }
    if (c->max_networks < 1 || c->max_networks > 32) {
        c->max_networks = 32;
    }
    if (c->log_level > 4) {
        c->log_level = 2;
    }
}

void app_config_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "nvs init failed (%s), using defaults", esp_err_to_name(err));
        set_defaults(&s_cfg);
        s_running_baud = s_cfg.baud;
        return;
    }

    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        set_defaults(&s_cfg);
        s_running_baud = s_cfg.baud;
        return;
    }

    app_config_t loaded;
    size_t sz = sizeof(loaded);
    err = nvs_get_blob(h, NVS_KEY, &loaded, &sz);
    nvs_close(h);

    if (err != ESP_OK || sz != sizeof(loaded)) {
        ESP_LOGW(TAG, "no stored config, writing defaults");
        set_defaults(&s_cfg);
        app_config_save();
    } else {
        s_cfg = loaded;
        sanitize(&s_cfg);
    }
    s_running_baud = s_cfg.baud;
}

app_config_t *app_config_get(void)
{
    return &s_cfg;
}

uint32_t app_config_running_baud(void)
{
    return s_running_baud;
}

esp_err_t app_config_save(void)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(h, NVS_KEY, &s_cfg, sizeof(s_cfg));
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);
    return err;
}

void app_config_to_json(cJSON *dst, uint32_t running_baud)
{
    if (dst == NULL) {
        return;
    }
    cJSON_AddNumberToObject(dst, "scan_interval_ms", (double)s_cfg.scan_interval_ms);
    cJSON_AddNumberToObject(dst, "heartbeat_ms", (double)s_cfg.heartbeat_ms);
    cJSON_AddNumberToObject(dst, "baud", (double)s_cfg.baud);
    cJSON_AddNumberToObject(dst, "running_baud", (double)running_baud);
    cJSON_AddBoolToObject(dst, "baud_pending", s_cfg.baud != running_baud);
    cJSON_AddNumberToObject(dst, "max_networks", (double)s_cfg.max_networks);
    cJSON_AddNumberToObject(dst, "log_level", (double)s_cfg.log_level);
    cJSON_AddBoolToObject(dst, "auto_scan", s_cfg.auto_scan != 0);
}

proto_err_t app_config_apply_json(const cJSON *params, app_config_t *out, bool *baud_changed)
{
    if (params == NULL || out == NULL || !cJSON_IsObject(params)) {
        return E_BAD_PARAM;
    }

    *out = s_cfg;
    if (baud_changed != NULL) {
        *baud_changed = false;
    }

    const cJSON *it = NULL;

    it = cJSON_GetObjectItemCaseSensitive(params, "scan_interval_ms");
    if (it != NULL) {
        if (!cJSON_IsNumber(it)) return E_BAD_PARAM;
        double v = cJSON_GetNumberValue(it);
        if (v < 1000 || v > 60000) return E_BAD_PARAM;
        out->scan_interval_ms = (uint32_t)v;
    }

    it = cJSON_GetObjectItemCaseSensitive(params, "heartbeat_ms");
    if (it != NULL) {
        if (!cJSON_IsNumber(it)) return E_BAD_PARAM;
        double v = cJSON_GetNumberValue(it);
        if (v < 1000 || v > 60000) return E_BAD_PARAM;
        out->heartbeat_ms = (uint32_t)v;
    }

    it = cJSON_GetObjectItemCaseSensitive(params, "max_networks");
    if (it != NULL) {
        if (!cJSON_IsNumber(it)) return E_BAD_PARAM;
        double v = cJSON_GetNumberValue(it);
        if (v < 1 || v > 64) return E_BAD_PARAM;
        out->max_networks = (uint8_t)v;
    }

    it = cJSON_GetObjectItemCaseSensitive(params, "log_level");
    if (it != NULL) {
        if (!cJSON_IsNumber(it)) return E_BAD_PARAM;
        double v = cJSON_GetNumberValue(it);
        if (v < 0 || v > 4) return E_BAD_PARAM;
        out->log_level = (uint8_t)v;
    }

    it = cJSON_GetObjectItemCaseSensitive(params, "auto_scan");
    if (it != NULL) {
        if (!cJSON_IsBool(it)) return E_BAD_PARAM;
        out->auto_scan = cJSON_IsTrue(it) ? 1 : 0;
    }

    it = cJSON_GetObjectItemCaseSensitive(params, "baud");
    if (it != NULL) {
        if (!cJSON_IsNumber(it)) return E_BAD_PARAM;
        double v = cJSON_GetNumberValue(it);
        if (!baud_is_valid((uint32_t)v)) return E_BAD_PARAM;
        /* Changing the line rate on the fly would strand the host: persist it
         * and apply on next boot. */
        if ((uint32_t)v != out->baud) {
            out->baud = (uint32_t)v;
            if (baud_changed != NULL) *baud_changed = true;
        }
    }

    return E_OK;
}
