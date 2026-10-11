#include <string.h>
#include "link_demux.h"

static const uint8_t MAGIC[3] = { 'A', 'U', 'D' };

void demux_init(demux_t *d)
{
    memset(d, 0, sizeof(*d));
    d->state = DEMUX_TEXT;
}

static void reset_line(demux_t *d)
{
    d->line_len = 0;
    d->overflow = false;
    d->hdr_idx = 0;
}

static void text_byte(demux_t *d, const demux_cb_t *cb, void *ctx, uint8_t b)
{
    if (d->line_len == 0 && !d->overflow) {
        if (b == MAGIC[d->hdr_idx]) {            /* possible start of an 'AUD' header */
            if (++d->hdr_idx == 3) {
                d->hdr_idx = 0;
                d->len_got = 0;
                d->state = DEMUX_LEN;
            }
            return;
        }
        if (d->hdr_idx > 0) {                    /* 'A' / 'AU' that never became 'AUD': noise */
            d->hdr_idx = 0;
            if (b == MAGIC[0]) {                 /* ... but this byte may itself start the real header */
                d->hdr_idx = 1;
                return;
            }
        }
    }

    if (b == '\n' || b == '\r') {
        if (!d->overflow && d->line_len > 0) {
            d->line[d->line_len] = '\0';
            cb->on_line(ctx, d->line);
        }
        reset_line(d);
    } else if (d->line_len < DEMUX_LINE_MAX - 1) {
        d->line[d->line_len++] = (char)b;
    } else {
        d->overflow = true;
    }
}

void demux_feed(demux_t *d, const demux_cb_t *cb, void *ctx, const uint8_t *data, size_t n)
{
    size_t i = 0;
    while (i < n) {
        switch (d->state) {
        case DEMUX_TEXT:
            text_byte(d, cb, ctx, data[i++]);
            break;

        case DEMUX_LEN:
            d->len_bytes[d->len_got++] = data[i++];
            if (d->len_got == 4) {
                uint32_t len = (uint32_t)d->len_bytes[0] | ((uint32_t)d->len_bytes[1] << 8) |
                               ((uint32_t)d->len_bytes[2] << 16) | ((uint32_t)d->len_bytes[3] << 24);
                if (len > 0 && len < DEMUX_MAX_TRACK) {
                    d->remaining = len;
                    d->discard = false;
                    d->state = DEMUX_AUDIO;
                    cb->on_track(ctx, len);
                } else {
                    d->state = DEMUX_TEXT;       /* absurd length: it was noise, not a header */
                    reset_line(d);
                }
            }
            break;

        case DEMUX_AUDIO: {
            size_t take = n - i;
            if (take > d->remaining) take = d->remaining;
            if (!d->discard && !cb->on_audio(ctx, data + i, take)) {
                d->discard = true;
            }
            i += take;
            d->remaining -= (uint32_t)take;
            if (d->remaining == 0) {
                d->state = DEMUX_TEXT;
                reset_line(d);
                if (cb->on_track_end) cb->on_track_end(ctx, !d->discard);
            }
            break;
        }
        }
    }
}

void demux_abort(demux_t *d, const demux_cb_t *cb, void *ctx)
{
    if (d->state == DEMUX_AUDIO && cb && cb->on_track_end) cb->on_track_end(ctx, false);
    d->state = DEMUX_TEXT;
    d->remaining = 0;
    d->discard = false;
    reset_line(d);
}
