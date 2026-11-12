#pragma once

/* Compile-time identity. Overridable from platformio.ini build_flags. */

#ifndef FW_VERSION
#define FW_VERSION "0.2.1"
#endif

#ifndef FW_BUILD_TAG
#define FW_BUILD_TAG "phase1"
#endif

#ifndef PROTO_VERSION
#define PROTO_VERSION 1
#endif

/* The diagnostic build is the same firmware plus UART breadcrumbs; tag it so a
 * capture can never be mistaken for a release image. */
#ifdef WFA_DIAG_BUILD
#undef FW_VERSION
#undef FW_BUILD_TAG
#define FW_VERSION   "0.2.1-diag"
#define FW_BUILD_TAG "diag"
#endif

/* Human-readable board identity. Reported by `hello` and `info` so the Android
 * app can warn if the firmware is talking about a different board than the one
 * the user selected. */
#define DEVICE_MODEL "ESP8684-DevKitM-1"
#define DEVICE_CHIP  "ESP32-C2"

/* UART0 defaults for ESP8684 (U0TXD / U0RXD). Do not change without rewiring. */
#define PIN_UART_TX 20
#define PIN_UART_RX 19

/* Default line rate. 115200 is the only rate proven reliable across all CH34x
 * and CP210x bridges; higher rates are opt-in via config.set (see PROTOCOL.md). */
#define DEFAULT_BAUD 115200
