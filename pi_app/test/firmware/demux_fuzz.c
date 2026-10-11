/* Host-side fuzz test for esp32_firmware/src/tubi_one_board/src/link_demux.c.
 *
 * Builds random byte streams that mix text commands, audio tracks (payloads deliberately full of
 * 'A','U','D', newlines and every byte value), line noise, bogus headers and over-long lines, delivers
 * them in random 1..300 byte chunks, and checks the demux emitted EXACTLY the events an independent
 * model predicts: commands in order, each track's length, a hash of its exact bytes, and refusals.
 *
 *   cc -I<repo>/esp32_firmware/src/tubi_one_board/include demux_fuzz.c <repo>/.../link_demux.c -o demux_fuzz
 *   ./demux_fuzz [iterations]        exit 0 = no mismatches
 */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "link_demux.h"

#define MAXLOG 300000
typedef struct { char s[MAXLOG]; size_t n; } log_t;
static void put(log_t *l, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int w = vsnprintf(l->s + l->n, MAXLOG - l->n, fmt, ap); va_end(ap);
    if (w > 0) l->n += (size_t)w;
}
static unsigned fnv(const uint8_t *p, size_t n, unsigned h) { for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= 16777619u; } return h; }

/* ---- the device under test, wired to a log ---- */
static log_t got; static unsigned cur_hash; static int track_no; static bool refuse_plan[256];
static void on_line(void *c, const char *l) { (void)c; put(&got, "L:%s|", l); }
static void on_track(void *c, uint32_t len) { (void)c; track_no++; put(&got, "T:%u|", len); cur_hash = 2166136261u; }
static bool on_audio(void *c, const uint8_t *d, size_t n) { (void)c; if (refuse_plan[track_no]) return false; cur_hash = fnv(d, n, cur_hash); return true; }
static void on_end(void *c, bool ok) { (void)c; if (ok) put(&got, "A:%08x|E:ok|", cur_hash); else put(&got, "E:bad|"); }
static const demux_cb_t CB = { on_line, on_track, on_audio, on_end };

static const char *CMDS[] = { "status", "stop", "run", "test", "head up", "arm down", "door open", "door close", "bin 3", "slot 2", "dispense 4", "lazy 90",
                             "x AUD y", "head AUD", "stop AUDAUD", "q\x01\x02AUD\x03" };  /* 'AUD' after the first byte is plain text */
static uint8_t stream[700000], payload[8000];

int main(int argc, char **argv) {
    int iters = argc > 1 ? atoi(argv[1]) : 20000;
    unsigned seed = 12345; long bad = 0, tracks_total = 0, lines_total = 0, refused_total = 0;
    static log_t want;
    for (int it = 0; it < iters; it++) {
        size_t sn = 0; want.n = got.n = 0; want.s[0] = got.s[0] = 0; track_no = -1;
        memset(refuse_plan, 0, sizeof refuse_plan);
        int items = 1 + rand_r(&seed) % 8, planned_tracks = 0;
        for (int k = 0; k < items; k++) {
            int kind = rand_r(&seed) % 100;
            if (kind < 35) {                                              /* plain command */
                const char *c = CMDS[rand_r(&seed) % (sizeof CMDS / sizeof *CMDS)];
                sn += (size_t)sprintf((char *)stream + sn, "%s\n", c); put(&want, "L:%s|", c); lines_total++;
            } else if (kind < 50) {                                       /* talk line, up to 64 words */
                char line[700]; int w = 1 + rand_r(&seed) % 64, off = sprintf(line, "talk");
                for (int j = 0; j < w; j++) off += sprintf(line + off, " %d", 50 + rand_r(&seed) % 4000);
                sn += (size_t)sprintf((char *)stream + sn, "%s\n", line); put(&want, "L:%s|", line); lines_total++;
            } else if (kind < 80) {                                       /* audio track with a hostile payload */
                uint32_t len = 1 + rand_r(&seed) % 7900;
                for (uint32_t j = 0; j < len; j++) {
                    int r = rand_r(&seed) % 40;
                    payload[j] = r == 0 ? '\n' : r == 1 ? 'A' : r == 2 ? 'U' : r == 3 ? 'D' : (uint8_t)rand_r(&seed);
                    if (j + 2 < len && rand_r(&seed) % 120 == 0) { payload[j] = 'A'; payload[j + 1] = 'U'; payload[j + 2] = 'D'; j += 2; }
                }
                memcpy(stream + sn, "AUD", 3); sn += 3;
                for (int b = 0; b < 4; b++) stream[sn++] = (len >> (8 * b)) & 255;
                memcpy(stream + sn, payload, len); sn += len;
                bool refuse = rand_r(&seed) % 10 == 0;                    /* consumer cannot keep up */
                refuse_plan[planned_tracks] = refuse;
                put(&want, "T:%u|", len);
                if (refuse) { put(&want, "E:bad|"); refused_total++; } else put(&want, "A:%08x|E:ok|", fnv(payload, len, 2166136261u));
                planned_tracks++; tracks_total++;
            } else if (kind < 88) {                                       /* line noise: stray 'A' / 'AU' then newline */
                sn += (size_t)sprintf((char *)stream + sn, "%s\n", rand_r(&seed) % 2 ? "A" : "AU");
            } else if (kind < 94) {                                       /* bogus header: zero length, then a real command */
                memcpy(stream + sn, "AUD\0\0\0\0", 7); sn += 7;
                sn += (size_t)sprintf((char *)stream + sn, "stop\n"); put(&want, "L:stop|"); lines_total++;
            } else {                                                      /* over-long line: dropped, then resync */
                size_t L = 520 + rand_r(&seed) % 200; for (size_t j = 0; j < L; j++) stream[sn++] = 'x'; stream[sn++] = '\n';
            }
        }
        demux_t d; demux_init(&d);
        for (size_t pos = 0; pos < sn;) {
            size_t chunk = 1 + rand_r(&seed) % 300; if (chunk > sn - pos) chunk = sn - pos;
            demux_feed(&d, &CB, NULL, stream + pos, chunk); pos += chunk;
        }
        if (strcmp(want.s, got.s) != 0) {
            if (bad < 3) printf("MISMATCH iter %d (stream %zu bytes)\n want: %.400s\n  got: %.400s\n", it, sn, want.s, got.s);
            bad++;
        }
    }
    printf("%ld mismatches in %d random streams (%ld commands, %ld tracks, %ld refused)\n", bad, iters, lines_total, tracks_total, refused_total);
    return bad ? 1 : 0;
}
