#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

/* Hardware-independent MP3 -> 8-bit unsigned mono PCM pipeline.
 * main.c supplies the two callbacks (UART in, stream buffer out). */
typedef struct {
    /* Pull up to `max` MP3 bytes, waiting at most `timeout_ms` (0 = don't wait).
     * Return the number of bytes copied (0 = nothing yet). */
    int  (*read)(void *ctx, uint8_t *dst, size_t max, uint32_t timeout_ms);
    /* Push 8-bit unsigned PCM (128 = silence). May block: that is the back-pressure
     * that makes the whole chain run at the DAC's pace. */
    void (*write)(void *ctx, const uint8_t *pcm, size_t n);
    void *ctx;
} mp3_pipe_cfg_t;

typedef struct {
    size_t   pcm_bytes;   /* 8-bit samples produced */
    int      hz;          /* sample rate found in the stream */
    int      channels;    /* channel count found in the stream */
    bool     aborted;     /* true if the sender stalled and we gave up */
} mp3_pipe_result_t;

/* Decode one track of `total_bytes` MP3 bytes (as announced in the stream header). */
mp3_pipe_result_t mp3_pipe_play(const mp3_pipe_cfg_t *cfg, uint32_t total_bytes);