#pragma once

/* Redirects ESP-IDF logging into protocol frames.
 *
 * Hard requirement, not a nicety: UART0 is both the protocol link and the ROM
 * console. A single raw ESP_LOGI() would inject a non-JSON line into the stream
 * and desynchronise the Android parser. Every log line therefore leaves the
 * device as {"type":"event.log","data":{"msg":"..."}}.
 *
 * Must be installed before serial_init() (early lines are simply dropped). */
void log_router_init(void);
