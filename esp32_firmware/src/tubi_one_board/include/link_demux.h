#pragma once
/* Splits the single Pi -> ESP32 serial stream into its two kinds of traffic:
 *
 *   text commands   "talk 300 250\n", "dispense 2\n", ...      (servo control, one per line)
 *   audio tracks    'A' 'U' 'D' | uint32 length (LE) | mp3 bytes
 *
 * Pure C with no FreeRTOS or driver calls, so it is unit-tested and fuzzed on a PC
 * (pi_app/test/test_firmware_demux.py). Feed it whatever bytes the UART delivers, in any chunking.
 *
 * Rules:
 *  - An 'AUD' header is only recognised at the START of a line (after a newline or at power-up), so
 *    "AUD" bytes inside a track's mp3 payload are never mistaken for a header: the payload is consumed
 *    by its announced length.
 *  - Text commands never begin with an uppercase 'A', so a stray 'A'/'AU' (line noise) at line start
 *    that does not complete into 'AUD' is silently dropped, the same resync the old audio firmware used.
 *  - A length of 0 or >= DEMUX_MAX_TRACK is treated as noise and ignored.
 */
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#define DEMUX_LINE_MAX  512
#define DEMUX_MAX_TRACK (64u * 1024u * 1024u)

typedef struct {
    /* One complete text command (no newline, NUL-terminated). */
    void (*on_line)(void *ctx, const char *line);
    /* An AUD header announced a track of `len` mp3 bytes. */
    void (*on_track)(void *ctx, uint32_t len);
    /* mp3 bytes of the current track. Return false if they could not be taken (the demux then discards
     * the rest of this track so the stream stays in sync). */
    bool (*on_audio)(void *ctx, const uint8_t *data, size_t n);
    /* The track's announced length has been consumed. ok=false if on_audio ever refused data. May be NULL. */
    void (*on_track_end)(void *ctx, bool ok);
} demux_cb_t;

typedef enum { DEMUX_TEXT, DEMUX_LEN, DEMUX_AUDIO } demux_state_t;

typedef struct {
    demux_state_t state;
    uint8_t  hdr_idx;                 /* how many of 'A','U','D' matched at the start of this line */
    uint8_t  len_bytes[4];
    uint8_t  len_got;
    uint32_t remaining;               /* audio bytes still to come in the current track */
    bool     discard;                 /* on_audio refused data: swallow the rest of the track */
    char     line[DEMUX_LINE_MAX];
    size_t   line_len;
    bool     overflow;                /* current line is longer than DEMUX_LINE_MAX */
} demux_t;

void demux_init(demux_t *d);
void demux_feed(demux_t *d, const demux_cb_t *cb, void *ctx, const uint8_t *data, size_t n);
/* The sender went quiet mid-header or mid-track: give up on it and listen for commands again. */
void demux_abort(demux_t *d, const demux_cb_t *cb, void *ctx);
