#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

/* ---------------------------------------------------------------------------
 * USBSerialManager (device side) — owns UART0.
 *
 * On ESP8684 there is no native USB, so the protocol rides on UART0 (GPIO20 TX
 * / GPIO19 RX) through the board's USB-to-UART bridge. UART0 is also the ROM
 * console, so log_router.c redirects IDF logging into JSON frames; this module
 * must therefore never allow raw printf output into the stream.
 *
 * Contract for the line handler: called from the RX task, one complete
 * CR/LF-free line at a time. It must not block for more than ~100 ms, or UART
 * RX overflow becomes likely. Long operations (Phase 2 scans) must be queued to
 * their own task and answered asynchronously.
 * ------------------------------------------------------------------------- */

typedef void (*serial_line_cb_t)(char *line, size_t len);

typedef struct {
    uint32_t baud;
    int      tx_pin;
    int      rx_pin;
} serial_cfg_t;

typedef struct {
    uint32_t bytes_rx;
    uint32_t bytes_tx;
    uint32_t lines_rx;
    uint32_t overflow;  /* inbound lines dropped for exceeding PROTO_MAX_INBOUND */
    uint32_t uart_err;  /* fifo overruns / framing / parity errors */
} serial_stats_t;

esp_err_t serial_init(const serial_cfg_t *cfg);
void      serial_set_line_handler(serial_line_cb_t cb);

/* Thread-safe write. Returns bytes actually queued. */
size_t serial_write(const void *buf, size_t len);

/* Writes `line` followed by a single '\n', atomically with respect to other
 * writers (single UART write lock), so frames never interleave. */
bool serial_write_line(const char *line);

/* Blocks until the TX ring is empty (or `timeout_ms` elapses). Needed before a
 * reset so the last frame actually leaves the wire. */
esp_err_t serial_wait_tx_done(uint32_t timeout_ms);

void serial_get_stats(serial_stats_t *out);
