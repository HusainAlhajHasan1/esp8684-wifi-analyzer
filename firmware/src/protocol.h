#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "cJSON.h"

/* ---------------------------------------------------------------------------
 * Protocol v1 — frame construction helpers.
 *
 * Every frame emitted by the device is a single line of UTF-8 JSON terminated
 * by '\n' and carries:  v (protocol version), id (request echo, 0 for
 * unsolicited), seq (monotonic frame counter), type, ts (device uptime ms).
 *
 * The `seq` counter is what lets the Android side PROVE the USB link is clean:
 * a gap means a dropped or corrupted frame, so the app can report link quality
 * instead of silently showing stale scan data.
 *
 * See docs/PROTOCOL.md for the normative spec.
 * ------------------------------------------------------------------------- */

#define PROTO_MAX_INBOUND 2048 /* max bytes in one inbound line (excl. CR/LF) */

typedef enum {
    E_OK = 0,
    E_BAD_JSON,
    E_BAD_FRAME,
    E_UNKNOWN_CMD,
    E_BAD_PARAM,
    E_TOO_LARGE,
    E_BUSY,
    E_NO_MEM,
    E_UNSUPPORTED,
    E_TIMEOUT,
    E_NO_DATA,   /* asked for cached scan results before any scan completed */
    E_INTERNAL,
} proto_err_t;

const char *proto_err_str(proto_err_t e);

/* Emit {"v":1,"id":id,"seq":n,"type":type,"ts":ms[,"data":data]}.
 * Takes ownership of `data` (may be NULL). Returns false on allocation or
 * write failure. Not thread-safe with respect to `seq` ordering across cores,
 * but ESP8684 is single-core and all emitters run in tasks that hold the UART
 * write mutex, so ordering is preserved. */
bool proto_emit(const char *type, int32_t id, cJSON *data);

/* Best-effort recovery of the request id from a line we cannot fully parse.
 *
 * Needed because an inbound line longer than PROTO_MAX_INBOUND is discarded
 * before JSON parsing runs - but the host is still waiting for a reply that
 * carries ITS id. Without this, a rejected oversize request looks to the host
 * like a dead link (the exact failure the 115200 probe hit: id 0 vs id 28).
 *
 * Only the retained prefix of the line is searched, and the first
 * "id" : <digits> pair wins. A literal "id" inside a string value earlier in
 * the line could be picked up instead - acceptable for a recovery path, since
 * the alternative is no reply at all. Returns 0 when nothing is found. */
int32_t proto_peek_id(const char *line, size_t len);

/* Emit an error frame: {"type":"error","code":"E_...","msg":"..."} */
bool proto_emit_error(int32_t id, proto_err_t err, const char *fmt, ...);

/* Convenience for unsolicited frames; prefixes nothing, caller passes
 * e.g. "event.heartbeat". Same ownership rules as proto_emit. */
bool proto_emit_event(const char *type, cJSON *data);
