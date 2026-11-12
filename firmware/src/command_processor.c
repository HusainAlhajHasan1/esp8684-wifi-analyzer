#include "command_processor.h"

#include "wifi_scanner.h"

#include <string.h>

#include "app_config.h"
#include "device_info.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "protocol.h"
#include "serial_manager.h"
#include "version.h"

static const char *TAG = "cmd";

#define MAX_ECHO_BYTES 1024

typedef struct {
    uint32_t rx_frames;
    uint32_t bad_json;
    uint32_t bad_frame;
    uint32_t unknown_cmd;
    uint32_t unsupported;
    uint32_t bad_param;
    uint32_t max_frame_len;
} cmd_stats_t;

static cmd_stats_t        s_stats;
static esp_timer_handle_t s_hb_timer;

typedef proto_err_t (*cmd_fn)(int32_t id, const char *name, const cJSON *params);

typedef struct {
    const char *name;
    cmd_fn      fn;
} cmd_entry_t;

/* ---------------------------------------------------------------------------
 * Handlers
 * ------------------------------------------------------------------------- */

static proto_err_t cmd_hello(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    (void)params;

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }


    cJSON_AddStringToObject(d, "device", DEVICE_MODEL);
    cJSON_AddStringToObject(d, "chip", DEVICE_CHIP);
    cJSON_AddStringToObject(d, "fw", FW_VERSION);
    cJSON_AddStringToObject(d, "build_tag", FW_BUILD_TAG);
    cJSON_AddNumberToObject(d, "proto", PROTO_VERSION);
    cJSON_AddNumberToObject(d, "phase", 2);
    cJSON_AddNumberToObject(d, "baud", (double)app_config_running_baud());
    cJSON_AddStringToObject(d, "ts_source", "uptime_ms");
    cJSON_AddStringToObject(d, "encoding", "json-lines/utf-8");

    static const char *supported[] = {
        "hello", "ping", "echo", "info", "stats",
        "config.get", "config.set", "hb.set", "reboot",
        "scan.start", "scan.stop", "scan.get", NULL
    };
    static const char *planned[] = {
        "ap.start", "ap.stop", NULL
    };

    cJSON *cmds = cJSON_AddArrayToObject(d, "commands");
    for (int i = 0; supported[i] != NULL; i++) {
        cJSON_AddItemToArray(cmds, cJSON_CreateString(supported[i]));
    }
    cJSON *fut = cJSON_AddArrayToObject(d, "planned");
    for (int i = 0; planned[i] != NULL; i++) {
        cJSON_AddItemToArray(fut, cJSON_CreateString(planned[i]));
    }

    cJSON *lim = cJSON_AddObjectToObject(d, "limits");
    if (lim != NULL) {
        cJSON_AddNumberToObject(lim, "max_inbound_bytes", PROTO_MAX_INBOUND);
        cJSON_AddNumberToObject(lim, "max_echo_bytes", MAX_ECHO_BYTES);
        cJSON_AddNumberToObject(lim, "max_networks", WFA_MAX_NETWORKS);
    }

    proto_emit("hello", id, d);
    return E_OK;
}

static proto_err_t cmd_ping(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    /* Host timestamps are echoed verbatim so RTT can be measured without
     * trusting the device clock (which is plain uptime, there is no RTC). */
    const cJSON *t = (params != NULL) ? cJSON_GetObjectItemCaseSensitive(params, "t") : NULL;
    if (cJSON_IsNumber(t)) {
        cJSON_AddNumberToObject(d, "t", cJSON_GetNumberValue(t));
    }
    cJSON_AddStringToObject(d, "pong", "ok");
    proto_emit("pong", id, d);
    return E_OK;
}

static proto_err_t cmd_echo(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    if (params == NULL) {
        return E_BAD_PARAM;
    }
    const cJSON *p = cJSON_GetObjectItemCaseSensitive(params, "payload");
    if (!cJSON_IsString(p)) {
        return E_BAD_PARAM;
    }
    size_t len = strlen(p->valuestring);
    if (len > MAX_ECHO_BYTES) {
        return E_TOO_LARGE;
    }

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    cJSON_AddStringToObject(d, "payload", p->valuestring);
    cJSON_AddNumberToObject(d, "len", (double)len);
    proto_emit("echo", id, d);
    return E_OK;
}

static proto_err_t cmd_info(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    (void)params;
    cJSON *d = device_info_build();
    if (d == NULL) {
        return E_NO_MEM;
    }
    proto_emit("info", id, d);
    return E_OK;
}

cJSON *command_processor_stats_build(void)
{
    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return NULL;
    }

    serial_stats_t ss;
    serial_get_stats(&ss);

    cJSON *link = cJSON_AddObjectToObject(d, "link");
    if (link != NULL) {
        cJSON_AddNumberToObject(link, "bytes_rx", (double)ss.bytes_rx);
        cJSON_AddNumberToObject(link, "bytes_tx", (double)ss.bytes_tx);
        cJSON_AddNumberToObject(link, "lines_rx", (double)ss.lines_rx);
        cJSON_AddNumberToObject(link, "overflow", (double)ss.overflow);
        cJSON_AddNumberToObject(link, "uart_err", (double)ss.uart_err);
    }

    cJSON *pr = cJSON_AddObjectToObject(d, "protocol");
    if (pr != NULL) {
        cJSON_AddNumberToObject(pr, "rx_frames", (double)s_stats.rx_frames);
        cJSON_AddNumberToObject(pr, "bad_json", (double)s_stats.bad_json);
        cJSON_AddNumberToObject(pr, "bad_frame", (double)s_stats.bad_frame);
        cJSON_AddNumberToObject(pr, "unknown_cmd", (double)s_stats.unknown_cmd);
        cJSON_AddNumberToObject(pr, "unsupported", (double)s_stats.unsupported);
        cJSON_AddNumberToObject(pr, "bad_param", (double)s_stats.bad_param);
        cJSON_AddNumberToObject(pr, "max_frame_len", (double)s_stats.max_frame_len);
    }

    cJSON *mem = cJSON_AddObjectToObject(d, "mem");
    if (mem != NULL) {
        cJSON_AddNumberToObject(mem, "heap_free", (double)esp_get_free_heap_size());
        cJSON_AddNumberToObject(mem, "heap_min_free", (double)esp_get_minimum_free_heap_size());
        cJSON_AddNumberToObject(mem, "heap_largest_block",
                                (double)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    }

    cJSON_AddNumberToObject(d, "uptime_ms", (double)(esp_timer_get_time() / 1000));
    cJSON_AddStringToObject(d, "reset_reason", device_reset_reason_str());
    return d;
}

static proto_err_t cmd_stats(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    (void)params;
    cJSON *d = command_processor_stats_build();
    if (d == NULL) {
        return E_NO_MEM;
    }
    proto_emit("stats", id, d);
    return E_OK;
}

static proto_err_t cmd_config_get(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    (void)params;
    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    app_config_to_json(d, app_config_running_baud());
    proto_emit("config", id, d);
    return E_OK;
}

static proto_err_t cmd_config_set(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    app_config_t next;
    bool baud_changed = false;

    proto_err_t rc = app_config_apply_json(params, &next, &baud_changed);
    if (rc != E_OK) {
        s_stats.bad_param++;
        return rc;
    }

    *app_config_get() = next;
    esp_err_t err = app_config_save();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "config save failed: %s", esp_err_to_name(err));
        return E_INTERNAL;
    }

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    app_config_to_json(d, app_config_running_baud());
    cJSON_AddBoolToObject(d, "saved", true);
    /* Tell the client the truth: a baud change is not live yet. */
    cJSON_AddStringToObject(d, "apply",
                            baud_changed ? "reboot_required_for_baud" : "applied");
    proto_emit("config", id, d);
    return E_OK;
}

static void hb_cb(void *arg)
{
    (void)arg;
    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return;
    }
    cJSON_AddNumberToObject(d, "uptime_ms", (double)(esp_timer_get_time() / 1000));
    cJSON_AddNumberToObject(d, "heap_free", (double)esp_get_free_heap_size());
    proto_emit_event("event.heartbeat", d);
}

void command_processor_heartbeat_set(uint32_t period_ms)
{
    if (period_ms < 1000) {
        period_ms = 1000;
    }
    if (s_hb_timer == NULL) {
        return;
    }
    esp_timer_stop(s_hb_timer);
    if (esp_timer_start_periodic(s_hb_timer, (uint64_t)period_ms * 1000ULL) != ESP_OK) {
        ESP_LOGW(TAG, "heartbeat restart failed");
    }
}

void command_processor_heartbeat_start(uint32_t period_ms)
{
    if (period_ms < 1000) {
        period_ms = 1000;
    }
    if (s_hb_timer == NULL) {
        esp_timer_create_args_t args = {
            .callback = hb_cb,
            .arg = NULL,
            .name = "hb",
        };
        if (esp_timer_create(&args, &s_hb_timer) != ESP_OK) {
            ESP_LOGE(TAG, "heartbeat timer create failed");
            return;
        }
    } else {
        esp_timer_stop(s_hb_timer);
    }
    esp_timer_start_periodic(s_hb_timer, (uint64_t)period_ms * 1000ULL);
}

static proto_err_t cmd_hb_set(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    if (params == NULL) {
        return E_BAD_PARAM;
    }
    const cJSON *it = cJSON_GetObjectItemCaseSensitive(params, "heartbeat_ms");
    if (!cJSON_IsNumber(it)) {
        return E_BAD_PARAM;
    }
    double v = cJSON_GetNumberValue(it);
    if (v < 1000 || v > 60000) {
        return E_BAD_PARAM;
    }

    app_config_get()->heartbeat_ms = (uint32_t)v;
    app_config_save();
    command_processor_heartbeat_set((uint32_t)v);

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    cJSON_AddNumberToObject(d, "heartbeat_ms", v);
    proto_emit("hb", id, d);
    return E_OK;
}

static proto_err_t cmd_reboot(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    (void)params;
    cJSON *d = cJSON_CreateObject();
    if (d != NULL) {
        cJSON_AddNumberToObject(d, "reboot_in_ms", 300);
        proto_emit("rebooting", id, d);
    }
    /* Let the frame reach the host before the UART dies with us. */
    serial_wait_tx_done(500);
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
    return E_OK; /* not reached */
}

static proto_err_t cmd_scan_start(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    bool continuous = false;
    uint32_t interval = app_config_get()->scan_interval_ms;

    if (params != NULL) {
        const cJSON *cont = cJSON_GetObjectItemCaseSensitive(params, "continuous");
        if (cJSON_IsBool(cont)) {
            continuous = cJSON_IsTrue(cont);
        }
        /* cJSON_IsNull(NULL) is FALSE, so `!cJSON_IsNull(x)` is true for a
         * MISSING key - the classic cJSON trap. Test for presence with != NULL. */
        const cJSON *iv = cJSON_GetObjectItemCaseSensitive(params, "interval_ms");
        if (iv != NULL) {
            if (!cJSON_IsNumber(iv) || iv->valuedouble < 1000 || iv->valuedouble > 60000) {
                return E_BAD_PARAM;
            }
            interval = (uint32_t)iv->valuedouble;
        }
    }

    uint32_t scan_id = 0;
    esp_err_t err = wifi_scanner_start(continuous, interval, &scan_id);
    if (err == ESP_ERR_INVALID_STATE) {
        return E_BUSY; /* a scan is already running */
    }
    if (err != ESP_OK) {
        return E_INTERNAL;
    }

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    cJSON_AddNumberToObject(d, "scan_id", scan_id);
    cJSON_AddBoolToObject(d, "continuous", continuous);
    cJSON_AddNumberToObject(d, "interval_ms", interval);
    proto_emit("scan_started", id, d);
    return E_OK;
}

static proto_err_t cmd_scan_stop(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    (void)params;
    esp_err_t err = wifi_scanner_stop();
    if (err != ESP_OK) {
        return E_INTERNAL;
    }
    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    cJSON_AddNumberToObject(d, "scan_id", wifi_scanner_last_scan_id());
    proto_emit("scan_stopped", id, d);
    return E_OK;
}

/* `scan.get` answers with current status. With params.replay=true it also
 * re-emits the cached result pages (same scan_id, cached:true) so a client that
 * lost a page can recover it WITHOUT triggering new RF activity. */
static proto_err_t cmd_scan_get(int32_t id, const char *name, const cJSON *params)
{
    (void)name;
    bool replay = false;
    if (params != NULL) {
        const cJSON *r = cJSON_GetObjectItemCaseSensitive(params, "replay");
        replay = cJSON_IsTrue(r);
    }

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return E_NO_MEM;
    }
    wifi_scanner_status_json(d);
    proto_emit("scan_status", id, d);

    if (replay && !wifi_scanner_emit_cached()) {
        proto_emit_error(id, E_NO_DATA, "no scan has completed yet");
    }
    return E_OK;
}

static proto_err_t cmd_planned(int32_t id, const char *name, const cJSON *params)
{
    (void)params;
    s_stats.unsupported++;
    /* Honest refusal beats a fabricated success: the app can grey these out. */
    proto_emit_error(id, E_UNSUPPORTED, "%s is not implemented in phase 1 firmware", name);
    return E_OK;
}

static const cmd_entry_t s_cmds[] = {
    { "hello",       cmd_hello      },
    { "ping",        cmd_ping       },
    { "echo",        cmd_echo       },
    { "info",        cmd_info       },
    { "stats",       cmd_stats      },
    { "config.get",  cmd_config_get },
    { "config.set",  cmd_config_set },
    { "hb.set",      cmd_hb_set     },
    { "reboot",      cmd_reboot     },
    { "scan.start",  cmd_scan_start },
    { "scan.stop",   cmd_scan_stop  },
    { "scan.get",    cmd_scan_get   },
    { "ap.start",    cmd_planned    },
    { "ap.stop",     cmd_planned    },
};

static const cmd_entry_t *find_cmd(const char *name)
{
    for (size_t i = 0; i < sizeof(s_cmds) / sizeof(s_cmds[0]); i++) {
        if (strcmp(s_cmds[i].name, name) == 0) {
            return &s_cmds[i];
        }
    }
    return NULL;
}

void command_processor_init(void)
{
    memset(&s_stats, 0, sizeof(s_stats));
}

void command_process_line(char *line, size_t len)
{
    if (line == NULL || len == 0) {
        return;
    }

    s_stats.rx_frames++;
    if (len > s_stats.max_frame_len) {
        s_stats.max_frame_len = (uint32_t)len;
    }

    cJSON *root = cJSON_Parse(line);
    if (root == NULL) {
        s_stats.bad_json++;
        proto_emit_error(0, E_BAD_JSON, "frame is not valid JSON");
        return;
    }

    const cJSON *jid = cJSON_GetObjectItemCaseSensitive(root, "id");
    int32_t id = cJSON_IsNumber(jid) ? (int32_t)cJSON_GetNumberValue(jid) : 0;

    const cJSON *vj = cJSON_GetObjectItemCaseSensitive(root, "v");
    if (cJSON_IsNumber(vj) && (int)cJSON_GetNumberValue(vj) > PROTO_VERSION) {
        s_stats.bad_frame++;
        proto_emit_error(id, E_UNSUPPORTED, "protocol v%d newer than device v%d",
                         (int)cJSON_GetNumberValue(vj), PROTO_VERSION);
        cJSON_Delete(root);
        return;
    }

    const cJSON *cmdj = cJSON_GetObjectItemCaseSensitive(root, "cmd");
    if (!cJSON_IsString(cmdj)) {
        s_stats.bad_frame++;
        proto_emit_error(id, E_BAD_FRAME, "missing string field 'cmd'");
        cJSON_Delete(root);
        return;
    }

    const char *name = cmdj->valuestring;
    const cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");

    const cmd_entry_t *e = find_cmd(name);
    proto_err_t rc;
    if (e == NULL) {
        s_stats.unknown_cmd++;
        rc = E_UNKNOWN_CMD;
    } else {
        rc = e->fn(id, name, params);
    }

    if (rc != E_OK) {
        proto_emit_error(id, rc, "%s failed: %s", name, proto_err_str(rc));
    }

    cJSON_Delete(root);
}
