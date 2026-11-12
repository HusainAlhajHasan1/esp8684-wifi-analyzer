#include "log_router.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "protocol.h"

#define LOG_MSG_MAX 192

static int json_log_vprintf(const char *fmt, va_list ap)
{
    char buf[LOG_MSG_MAX];
    va_list ap2;
    va_copy(ap2, ap);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap2);
    va_end(ap2);

    if (n < 0) {
        return n;
    }

    /* Trim the trailing newline ESP_LOG adds; the framer supplies its own. */
    size_t len = strlen(buf);
    while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r')) {
        buf[--len] = '\0';
    }
    if (len == 0) {
        return n;
    }

    cJSON *d = cJSON_CreateObject();
    if (d == NULL) {
        return n;
    }
    cJSON_AddStringToObject(d, "msg", buf);
    proto_emit_event("event.log", d);
    return n;
}

void log_router_init(void)
{
    esp_log_set_vprintf(json_log_vprintf);
}
