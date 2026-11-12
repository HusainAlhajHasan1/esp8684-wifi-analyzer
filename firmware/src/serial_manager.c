#include "serial_manager.h"

#include <string.h>

#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "protocol.h"
#include "version.h"

static const char *TAG = "serial"; /* used only through the JSON log router */

#define UART_PORT   UART_NUM_0
#define RX_RING     4096
#define TX_RING     8192
#define EVT_QUEUE   20
#define RX_TASK_STACK 4096
#define RX_CHUNK    256
#define RX_TASK_PRIO 12
#define TX_LOCK_MS  1000

static SemaphoreHandle_t s_tx_lock;
static QueueHandle_t     s_evt_queue;
static serial_line_cb_t  s_line_cb;
static serial_stats_t    s_stats;
static TaskHandle_t      s_rx_task;

/* Assembled on a static buffer, not the task stack: 2 KB of stack for a line
 * buffer would be a large slice of ESP8684's SRAM budget. */
static char s_line[PROTO_MAX_INBOUND + 1];

static void serial_rx_task(void *arg)
{
    (void)arg;
    size_t n = 0;
    bool dropping = false;
    uint8_t chunk[RX_CHUNK];
    uart_event_t ev;

    for (;;) {
        if (xQueueReceive(s_evt_queue, &ev, pdMS_TO_TICKS(1000)) != pdTRUE) {
            continue;
        }

        switch (ev.type) {
        case UART_DATA: {
            uint32_t remaining = ev.size;
            while (remaining > 0) {
                int want = (remaining > RX_CHUNK) ? RX_CHUNK : (int)remaining;
                int got = uart_read_bytes(UART_PORT, chunk, want, pdMS_TO_TICKS(100));
                if (got <= 0) {
                    break;
                }
                remaining -= (uint32_t)got;
                s_stats.bytes_rx += (uint32_t)got;

                for (int i = 0; i < got; i++) {
                    char c = (char)chunk[i];

                    if (c == '\n' || c == '\r') {
                        if (dropping) {
                            /* Counter was already bumped where the overflow
                             * was detected - one per rejected line. */
                            dropping = false;
                        }
                        if (n > 0) {
                            s_line[n] = '\0';
                            s_stats.lines_rx++;
                            if (s_line_cb != NULL) {
                                s_line_cb(s_line, n);
                            }
                            n = 0;
                        }
                        continue;
                    }

                    if (dropping) {
                        continue;
                    }
                    /* Tolerate stray control bytes (e.g. host line noise). */
                    if ((unsigned char)c < 0x20 && c != '\t') {
                        continue;
                    }
                    if (n >= PROTO_MAX_INBOUND) {
                        /* Reject, but answer with the host's own request id so
                         * the host can match the error to its request instead
                         * of timing out on a reply addressed to id 0. */
                        s_line[n] = '\0';
                        int32_t overlong_id = proto_peek_id(s_line, n);
                        dropping = true;
                        n = 0;
                        s_stats.overflow++;
                        proto_emit_error(overlong_id, E_TOO_LARGE,
                                         "inbound frame exceeds %d bytes", PROTO_MAX_INBOUND);
                        continue;
                    }
                    s_line[n++] = c;
                }
            }
            break;
        }

        case UART_FIFO_OVF:
        case UART_BUFFER_FULL:
            /* Host pumped faster than we drained. Resync rather than die. */
            uart_flush_input(UART_PORT);
            xQueueReset(s_evt_queue);
            n = 0;
            dropping = false;
            s_stats.uart_err++;
            proto_emit_error(0, E_INTERNAL, "uart rx overflow, input flushed");
            break;

        case UART_PARITY_ERR:
        case UART_FRAME_ERR:
            s_stats.uart_err++;
            break;

        default:
            break;
        }
    }
}

esp_err_t serial_init(const serial_cfg_t *cfg)
{
    if (cfg == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    uart_config_t uc = {
        .baud_rate  = (int)cfg->baud,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    esp_err_t err = uart_param_config(UART_PORT, &uc);
    if (err != ESP_OK) {
        return err;
    }

    /* ESP8684 UART0 defaults, kept explicit so a wrong board revision is a
     * compile-time-visible change rather than a silent one. */
    err = uart_set_pin(UART_PORT, cfg->tx_pin, cfg->rx_pin,
                       UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        return err;
    }

    err = uart_driver_install(UART_PORT, RX_RING, TX_RING, EVT_QUEUE, &s_evt_queue, 0);
    if (err != ESP_OK) {
        return err;
    }

    s_tx_lock = xSemaphoreCreateMutex();
    if (s_tx_lock == NULL) {
        return ESP_ERR_NO_MEM;
    }

    if (xTaskCreate(serial_rx_task, "uart_rx", RX_TASK_STACK, NULL, RX_TASK_PRIO,
                    &s_rx_task) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }

    /* Drop anything the ROM bootloader printed before our driver existed. */
    uart_flush_input(UART_PORT);
    xQueueReset(s_evt_queue);

    ESP_LOGI(TAG, "uart0 ready: %lu baud, tx=%d rx=%d",
             (unsigned long)cfg->baud, cfg->tx_pin, cfg->rx_pin);
    return ESP_OK;
}

void serial_set_line_handler(serial_line_cb_t cb)
{
    s_line_cb = cb;
}

size_t serial_write(const void *buf, size_t len)
{
    if (buf == NULL || len == 0 || s_tx_lock == NULL) {
        return 0;
    }
    if (xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(TX_LOCK_MS)) != pdTRUE) {
        return 0;
    }
    int written = uart_write_bytes(UART_PORT, buf, len);
    xSemaphoreGive(s_tx_lock);
    if (written > 0) {
        s_stats.bytes_tx += (uint32_t)written;
        return (size_t)written;
    }
    return 0;
}

bool serial_write_line(const char *line)
{
    if (line == NULL || s_tx_lock == NULL) {
        return false;
    }
    size_t len = strlen(line);
    if (len == 0) {
        return false;
    }
    if (xSemaphoreTake(s_tx_lock, pdMS_TO_TICKS(TX_LOCK_MS)) != pdTRUE) {
        return false;
    }
    int a = uart_write_bytes(UART_PORT, line, len);
    int b = uart_write_bytes(UART_PORT, "\n", 1);
    xSemaphoreGive(s_tx_lock);

    if (a > 0) {
        s_stats.bytes_tx += (uint32_t)a;
    }
    if (b > 0) {
        s_stats.bytes_tx += (uint32_t)b;
    }
    return (a == (int)len) && (b == 1);
}

esp_err_t serial_wait_tx_done(uint32_t timeout_ms)
{
    return uart_wait_tx_done(UART_PORT, pdMS_TO_TICKS(timeout_ms));
}

void serial_get_stats(serial_stats_t *out)
{
    if (out != NULL) {
        *out = s_stats;
    }
}
