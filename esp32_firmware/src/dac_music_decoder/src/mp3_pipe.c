#include <string.h>
#include "mp3_pipe.h"

#define MINIMP3_IMPLEMENTATION
#define MINIMP3_ONLY_MP3
#include "Minimp3.h"   /* must match the file name exactly: Linux file systems are case-sensitive */

#define MP3_BUF_SIZE     (16 * 1024)   /* minimp3 wants a generous window to find sync   */
#define REFILL_MIN_ROOM  4096          /* only top up when at least this much is free    */
#define STARVED_BYTES    2048          /* below this we wait for data instead of polling */
#define STALL_POLLS      25            /* 25 x 200 ms = 5 s without data -> give up      */

static mp3dec_t s_dec;
static uint8_t  s_in[MP3_BUF_SIZE];
static int16_t  s_pcm[MINIMP3_MAX_SAMPLES_PER_FRAME];
static uint8_t  s_out[MINIMP3_MAX_SAMPLES_PER_FRAME / 2];

mp3_pipe_result_t mp3_pipe_play(const mp3_pipe_cfg_t *cfg, uint32_t total_bytes)
{
    mp3_pipe_result_t res = {0};
    mp3dec_frame_info_t info;
    uint32_t remaining = total_bytes;   /* bytes of this track not yet pulled from the link */
    size_t   in_len = 0;                /* bytes currently sitting in s_in */
    int      idle = 0;

    mp3dec_init(&s_dec);

    for (;;) {
        /* 1. Top up the input window */
        size_t room = MP3_BUF_SIZE - in_len;
        if (remaining > 0 && room >= REFILL_MIN_ROOM) {
            size_t want = remaining < room ? remaining : room;
            uint32_t wait_ms = (in_len < STARVED_BYTES) ? 200 : 0;
            int n = cfg->read(cfg->ctx, s_in + in_len, want, wait_ms);
            if (n > 0) {
                in_len += (size_t)n;
                remaining -= (uint32_t)n;
                idle = 0;
            } else if (wait_ms > 0 && ++idle >= STALL_POLLS) {
                res.aborted = true;
                break;
            }
        }

        if (in_len == 0 && remaining == 0) {
            break;                                   /* all data decoded */
        }

        /* 2. Decode one frame */
        int samples = mp3dec_decode_frame(&s_dec, s_in, (int)in_len, s_pcm, &info);

        if (info.frame_bytes > 0) {                  /* consumed a frame (or skipped junk/tags) */
            in_len -= (size_t)info.frame_bytes;
            memmove(s_in, s_in + info.frame_bytes, in_len);
        } else if (remaining == 0) {
            break;                                   /* leftover = incomplete last frame */
        } else {
            if (room < REFILL_MIN_ROOM && in_len > 0) {   /* never spin on bad data */
                in_len--;
                memmove(s_in, s_in + 1, in_len);
            }
            continue;                                /* need more data */
        }

        /* 3. 16-bit signed (mono or stereo) -> 8-bit unsigned mono */
        if (samples > 0) {
            res.hz = info.hz;
            res.channels = info.channels;
            for (int i = 0; i < samples; i++) {
                int v = (info.channels == 2) ? (s_pcm[2 * i] + s_pcm[2 * i + 1]) / 2
                                             : s_pcm[i];
                s_out[i] = (uint8_t)((v >> 8) + 128);
            }
            cfg->write(cfg->ctx, s_out, (size_t)samples);
            res.pcm_bytes += (size_t)samples;
        }
    }
    return res;
}