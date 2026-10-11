/* A thin C shim around esp32_firmware/src/tubi_one_board/src/link_demux.c so Python (ctypes) can feed the
 * REAL firmware parser the exact bytes the Pi writes, and read back what it recognised. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "link_demux.h"

static char logbuf[1 << 20]; static size_t logn; static unsigned h;
static void app(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
#include <stdarg.h>
static void app(const char *fmt, ...) { va_list ap; va_start(ap, fmt); int w = vsnprintf(logbuf + logn, sizeof logbuf - logn, fmt, ap); va_end(ap); if (w > 0) logn += (size_t)w; }
static void on_line(void *c, const char *l) { (void)c; app("L:%s|", l); }
static void on_track(void *c, uint32_t len) { (void)c; app("T:%u|", len); h = 2166136261u; }
static bool on_audio(void *c, const uint8_t *d, size_t n) { (void)c; for (size_t i = 0; i < n; i++) { h ^= d[i]; h *= 16777619u; } return true; }
static void on_end(void *c, bool ok) { (void)c; if (ok) app("A:%08x|E:ok|", h); else app("E:bad|"); }
static const demux_cb_t CB = { on_line, on_track, on_audio, on_end };

void *shim_new(void) { demux_t *d = malloc(sizeof *d); demux_init(d); logn = 0; logbuf[0] = 0; return d; }
void shim_free(void *d) { free(d); }
void shim_feed(void *d, const uint8_t *p, size_t n) { demux_feed((demux_t *)d, &CB, NULL, p, n); }
const char *shim_log(void) { logbuf[logn] = 0; return logbuf; }
