#include "protocol.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_timer.h"
#include "serial_manager.h"
#include "version.h"

static uint32_t s_seq;

const char *proto_err_str(proto_err_t e)
{
    switch (e) {
    case E_OK:          return "E_OK";
    case E_BAD_JSON:    return "E_BAD_JSON";
    case E_BAD_FRAME:   return "E_BAD_FRAME";
    case E_UNKNOWN_CMD: return "E_UNKNOWN_CMD";
    case E_BAD_PARAM:   return "E_BAD_PARAM";
    case E_TOO_LARGE:   return "E_TOO_LARGE";
    case E_BUSY:        return "E_BUSY";
    case E_NO_MEM:      return "E_NO_MEM";
    case E_UNSUPPORTED: return "E_UNSUPPORTED";
    case E_TIMEOUT:     return "E_TIMEOUT";
    case E_NO_DATA:     return "E_NO_DATA";
    case E_INTERNAL:
    default:            return "E_INTERNAL";
    }
}

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Serialise, free, write. `root` ownership is consumed either way. */
static bool emit_root(cJSON *root)
{
    if (root == NULL) {
        return false;
    }
    char *txt = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (txt == NULL) {
        return false;
    }
    bool ok = serial_write_line(txt);
    /* cJSON in ESP-IDF allocates via malloc(); cJSON_free() is available in the
     * bundled version but malloc/free is used here to avoid version skew. */
    free(txt);
    return ok;
}

bool proto_emit(const char *type, int32_t id, cJSON *data)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        if (data != NULL) {
            cJSON_Delete(data);
        }
        return false;
    }
    cJSON_AddNumberToObject(root, "v", PROTO_VERSION);
    cJSON_AddNumberToObject(root, "id", (double)id);
    cJSON_AddNumberToObject(root, "seq", (double)(++s_seq));
    cJSON_AddStringToObject(root, "type", type);
    cJSON_AddNumberToObject(root, "ts", (double)now_ms());
    if (data != NULL) {
        cJSON_AddItemToObject(root, "data", data);
    }
    return emit_root(root);
}

bool proto_emit_event(const char *type, cJSON *data)
{
    return proto_emit(type, 0, data);
}

int32_t proto_peek_id(const char *line, size_t len)
{
    if (line == NULL || len == 0) {
        return 0;
    }
    for (size_t i = 0; i + 4 < len; i++) {
        if (line[i] != '"' || line[i + 1] != 'i' || line[i + 2] != 'd' || line[i + 3] != '"') {
            continue;
        }
        size_t j = i + 4;
        while (j < len && (line[j] == ' ' || line[j] == '\t')) {
            j++;
        }
        if (j >= len || line[j] != ':') {
            continue;
        }
        j++;
        while (j < len && (line[j] == ' ' || line[j] == '\t')) {
            j++;
        }
        bool negative = false;
        if (j < len && line[j] == '-') {
            negative = true;
            j++;
        }
        if (j >= len || line[j] < '0' || line[j] > '9') {
            continue;
        }
        int64_t value = 0;
        while (j < len && line[j] >= '0' && line[j] <= '9') {
            /* Saturating accumulate: a hostile 2000-digit "id" must not wrap. */
            if (value <= 1000000000000LL) {
                value = value * 10 + (line[j] - '0');
            }
            j++;
        }
        if (value > INT32_MAX) {
            value = INT32_MAX;
        }
        return (int32_t)(negative ? -value : value);
    }
    return 0;
}

bool proto_emit_error(int32_t id, proto_err_t err, const char *fmt, ...)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return false;
    }
    cJSON_AddNumberToObject(root, "v", PROTO_VERSION);
    cJSON_AddNumberToObject(root, "id", (double)id);
    cJSON_AddNumberToObject(root, "seq", (double)(++s_seq));
    cJSON_AddStringToObject(root, "type", "error");
    cJSON_AddNumberToObject(root, "ts", (double)now_ms());
    cJSON_AddStringToObject(root, "code", proto_err_str(err));
    if (fmt != NULL) {
        char msg[192];
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(msg, sizeof(msg), fmt, ap);
        va_end(ap);
        cJSON_AddStringToObject(root, "msg", msg);
    }
    return emit_root(root);
}
