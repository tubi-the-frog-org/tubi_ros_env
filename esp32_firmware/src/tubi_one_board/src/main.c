/*
 * Tubi on ONE ESP32 (DOIT DevKit V1, ESP32-D0WD-V3): MP3 audio out of the DAC, four servos, both
 * driven by the Raspberry Pi over a single UART.  ESP-IDF 5.x
 *
 *   Wiring (3.3 V logic, shared GND):
 *     Pi GPIO14 TXD (header pin 8)   ->  GPIO16   UART2 RX    audio + commands in
 *     Pi GPIO15 RXD (header pin 10)  <-  GPIO17   UART2 TX    replies + the end-of-clip ack
 *     Pi GPIO18     (header pin 12)  <-  GPIO18   UART2 RTS   flow control: this board drives it HIGH
 *                                                             to say "pause". The Pi must leave GPIO18
 *                                                             an INPUT (the Pi software paces itself).
 *     GPIO25  DAC out -> LM386 amplifier -> speaker
 *     GPIO15  head servo     GPIO2  arm servo     GPIO21  lazy susan     GPIO19  trap door
 *     UART0 (USB)  console + bench-test command port, 115200
 *
 *   One byte stream from the Pi carries two kinds of traffic (see link_demux.h):
 *     audio   'A' 'U' 'D' | uint32 length (little-endian) | MP3 bytes        -> decoded and played
 *     text    "talk 300 250 ...\n", "dispense 2\n", ...                      -> servos_handle_line()
 *   When a clip has finished playing this board sends one ACK byte (0x06) back; the Pi waits for it so
 *   "Tubi is speaking" lasts exactly as long as the sound. Text replies come back on the same wire.
 *
 *   MP3 must be 22050 Hz (the DAC rate): the Pi requests ElevenLabs "mp3_22050_32".
 *
 *   FreeRTOS tasks (core, priority):
 *     link_rx  (0, 6)  read UART2, demux text vs audio, feed the audio stream buffer / run commands
 *     tx       (0, 3)  the only writer of the UARTs: replies and the ack
 *     usb_cmd  (0, 4)  read USB console lines -> servos_handle_line()
 *     player   (1, 5)  minimp3 decode -> pcm_stream            (24 KB stack: minimp3 needs ~20 KB)
 *     dac_feed (any, 6) refill the DAC DMA buffers from pcm_stream
 *     servo    (1, 6)  slew the servos every 20 ms           \  both in servos.c
 *     anim     (1, 5)  talk / dispense state machines         /
 *
 *   Data path for audio:
 *     UART2 RX ring (32 KB, hardware RTS) -> link_rx -> audio_in (16 KB) -> player -> pcm_stream (16 KB)
 *       -> dac_feed -> DAC DMA -> GPIO25
 */
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/stream_buffer.h"
#include "driver/uart.h"
#include "driver/dac_continuous.h"
#include "esp_log.h"
#include "esp_system.h"
#include "mp3_pipe.h"
#include "link_demux.h"
#include "servos.h"
#include "tubi_link.h"

static const char *uart_tag = "LINK";
static const char *dac_tag  = "DAC";

/* ---- Pi link (UART2) --------------------------------------------------- */
#define LINK_UART      UART_NUM_2
#define LINK_RX_GPIO   16          /* <- Pi GPIO14 (TXD) */
#define LINK_TX_GPIO   17          /* -> Pi GPIO15 (RXD) */
#define LINK_RTS_GPIO  18          /* -> Pi GPIO18: HIGH = "pause sending" */
#define LINK_BAUD      921600
#define LINK_RX_BUF    (32 * 1024) /* ~8 s of audio at 32 kbit/s */
#define ACK_BYTE       0x06        /* "clip finished playing" -- the Pi is configured with AUDIO_ACK_BYTE=6 */

/* ---- USB console (UART0) ------------------------------------------------- */
#define USB_UART       UART_NUM_0
#define USB_BAUD       115200
#define USB_LINE_MAX   256

/* ---- DAC parameters for AUDIO --------------------------------------------- */
#define DAC_FREQ_HZ    22050       /* must match the MP3's sample rate */
#define DAC_DESC_NUM   6           /* docs suggest more than 5 so the DMA never starves */
#define DAC_BUF_SIZE   2048

#define PCM_STREAM_SIZE (16 * 1024)   /* 8-bit samples: ~0.74 s at 22.05 kHz */
#define PREFILL_BYTES   (8 * 1024)    /* start playing once this much PCM is queued */
#define DMA_TAIL_MS     ((DAC_DESC_NUM * DAC_BUF_SIZE * 1000) / DAC_FREQ_HZ + 50)

#define AUDIO_IN_SIZE   (16 * 1024)   /* mp3 bytes between link_rx and the player */
#define AUDIO_STALL_MS  3000          /* the player is not draining audio_in: give up on this track */
#define LINK_IDLE_MS    3000          /* sender went quiet mid-header / mid-track: resync */

static dac_continuous_handle_t asynch_continous = NULL;
static QueueHandle_t dac_evt_queue = NULL;
static StreamBufferHandle_t pcm_stream = NULL;   /* decoder writes 8-bit unsigned PCM here (128 = silence) */

static volatile bool s_prefilled = false;   /* true while the DAC is draining the stream */
static volatile bool s_flush     = false;   /* true at end of track: play out whatever is left */

static StreamBufferHandle_t audio_in = NULL;     /* mp3 bytes of the current track */
static QueueHandle_t        track_q  = NULL;     /* uint32_t track lengths announced by 'AUD' headers */
static volatile bool        track_accepted = false;  /* the current announced track has a taker */
static volatile bool        track_dead     = false;  /* the player gave up on it: discard the rest */

static TaskHandle_t h_rx, h_tx, h_usb, h_player;

/* ---- DAC ------------------------------------------------------------- */

// ISR: runs when the DMA finishes one buffer. Just forward the event.
static bool IRAM_ATTR dac_on_convert_done(dac_continuous_handle_t handle,
                                          const dac_event_data_t *event,
                                          void *user_data)
{
    QueueHandle_t q = (QueueHandle_t)user_data;
    BaseType_t woken = pdFALSE;
    if (xQueueIsQueueFullFromISR(q)) {          // drop oldest if we fell behind
        dac_event_data_t dummy;
        xQueueReceiveFromISR(q, &dummy, &woken);
    }
    xQueueSendFromISR(q, event, &woken);
    return woken == pdTRUE;
}

// Task: refill each finished DMA buffer with PCM (or silence).
// NOTE: on the original ESP32 the driver expands 8-bit samples to 16-bit, so a DMA buffer
// holds only buf_size/2 samples. dac_continuous_write_asynchronously() reports how many
// bytes it really took in `loaded`; whatever it didn't take must be kept for the next
// buffer, otherwise audio is silently dropped and the track plays too fast (and then
// starves, giving sped-up bursts with gaps).
static void dac_feed_task(void *arg)
{
    static uint8_t stage[DAC_BUF_SIZE];     // real PCM first, silence padding after it
    static uint8_t silence[DAC_BUF_SIZE];
    memset(silence, 128, sizeof(silence));  // 128 = mid-scale silence
    size_t staged = 0;                      // real PCM bytes waiting in stage[]
    dac_event_data_t evt;

    while (1) {
        xQueueReceive(dac_evt_queue, &evt, portMAX_DELAY);

        /* Don't start (or restart after an underrun) until there's a cushion of PCM,
         * unless the track is over and we just need to play out the tail. */
        if (!s_prefilled &&
            (s_flush || staged + xStreamBufferBytesAvailable(pcm_stream) >= PREFILL_BYTES)) {
            s_prefilled = true;
        }

        size_t loaded = 0;
        if (s_prefilled) {
            staged += xStreamBufferReceive(pcm_stream, stage + staged, DAC_BUF_SIZE - staged, 0);
            memset(stage + staged, 128, DAC_BUF_SIZE - staged);          // pad with silence
            dac_continuous_write_asynchronously(asynch_continous, evt.buf, evt.buf_size,
                                                stage, DAC_BUF_SIZE, &loaded);
            if (loaded > staged) {          // the DMA swallowed padding: we ran dry
                loaded = staged;
                s_prefilled = false;        // re-buffer before resuming
            }
            staged -= loaded;               // keep what the DMA didn't take
            memmove(stage, stage + loaded, staged);
        } else {
            dac_continuous_write_asynchronously(asynch_continous, evt.buf, evt.buf_size,
                                                silence, DAC_BUF_SIZE, &loaded);
        }
    }
}

void dac_init(void)
{
    dac_continuous_config_t cfg = {
        .chan_mask = DAC_CHANNEL_MASK_CH0,       // GPIO25
        .desc_num  = DAC_DESC_NUM,
        .buf_size  = DAC_BUF_SIZE,
        .freq_hz   = DAC_FREQ_HZ,
        .offset    = 0,
        .clk_src   = DAC_DIGI_CLK_SRC_DEFAULT,
        .chan_mode = DAC_CHANNEL_MODE_SIMUL,
    };
    ESP_ERROR_CHECK(dac_continuous_new_channels(&cfg, &asynch_continous));

    dac_evt_queue = xQueueCreate(DAC_DESC_NUM, sizeof(dac_event_data_t));
    pcm_stream    = xStreamBufferCreate(PCM_STREAM_SIZE, 1);

    dac_event_callbacks_t cbs = {
        .on_convert_done = dac_on_convert_done,
        .on_stop = NULL,
    };
    ESP_ERROR_CHECK(dac_continuous_register_event_callback(asynch_continous, &cbs, dac_evt_queue));

    ESP_ERROR_CHECK(dac_continuous_enable(asynch_continous));
    ESP_ERROR_CHECK(dac_continuous_start_async_writing(asynch_continous));

    xTaskCreate(dac_feed_task, "dac_feed", 4096, NULL, 6, NULL);
    ESP_LOGI(dac_tag, "DAC running at %d Hz", DAC_FREQ_HZ);
}


/* ---- Replies out: one queue, one writer ------------------------------------- */

typedef struct { uint8_t len; uint8_t to_usb; uint8_t data[160]; } tx_msg_t;
static QueueHandle_t tx_q = NULL;

static void tx_enqueue(const void *data, size_t n, bool to_usb)
{
    tx_msg_t m;
    if (n > sizeof(m.data)) n = sizeof(m.data);
    memcpy(m.data, data, n);
    m.len = (uint8_t)n;
    m.to_usb = to_usb;
    xQueueSend(tx_q, &m, 0);          /* never block a caller; a full queue drops this one */
}

void tubi_link_send_text(const char *text, size_t n) { tx_enqueue(text, n, true); }

static void tx_task(void *arg)
{
    tx_msg_t m;
    for (;;) {
        if (xQueueReceive(tx_q, &m, portMAX_DELAY) != pdTRUE) continue;
        uart_write_bytes(LINK_UART, m.data, m.len);
        if (m.to_usb) uart_write_bytes(USB_UART, m.data, m.len);
    }
}

void tubi_report_tasks(void)
{
    unsigned sv, an;
    servos_stack_free(&sv, &an);
    char buf[160];
    int n = snprintf(buf, sizeof(buf), "stack free: servo=%u anim=%u rx=%u tx=%u usb=%u player=%u (heap free=%u)\n",
                     sv, an, (unsigned)uxTaskGetStackHighWaterMark(h_rx), (unsigned)uxTaskGetStackHighWaterMark(h_tx),
                     (unsigned)uxTaskGetStackHighWaterMark(h_usb), (unsigned)uxTaskGetStackHighWaterMark(h_player),
                     (unsigned)esp_get_free_heap_size());
    if (n > 0) tubi_link_send_text(buf, (size_t)n);
}

/* ---- Pi link in: demux text commands vs audio tracks --------------------------- */

static void link_init(void)
{
    uart_config_t cfg = {
        .baud_rate  = LINK_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        /* RTS goes high when our RX FIFO nears full, telling the Pi to pause. The FIFO only backs up
         * when the 32 KB ring buffer is full, which is the intended back-pressure. */
        .flow_ctrl  = UART_HW_FLOWCTRL_RTS,
        .rx_flow_ctrl_thresh = 100,              /* of the 128-byte hardware FIFO */
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(LINK_UART, LINK_RX_BUF, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(LINK_UART, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(LINK_UART, LINK_TX_GPIO, LINK_RX_GPIO,
                                 LINK_RTS_GPIO, UART_PIN_NO_CHANGE));
    ESP_LOGI(uart_tag, "Pi link ready: UART2 RX=%d TX=%d RTS=%d @ %d baud",
             LINK_RX_GPIO, LINK_TX_GPIO, LINK_RTS_GPIO, LINK_BAUD);
}

static void on_line(void *ctx, const char *line)
{
    char buf[DEMUX_LINE_MAX];
    strncpy(buf, line, sizeof(buf) - 1);        /* servos_handle_line edits the line in place */
    buf[sizeof(buf) - 1] = '\0';
    servos_handle_line(buf);
}

static void on_track(void *ctx, uint32_t len)
{
    track_dead = false;
    track_accepted = (xQueueSend(track_q, &len, 0) == pdTRUE);   /* false: the player is still busy */
    if (!track_accepted) ESP_LOGW(uart_tag, "Track of %u bytes announced while the player is busy: dropped", (unsigned)len);
    else                 ESP_LOGI(uart_tag, "Track announced: %u bytes", (unsigned)len);
}

static bool on_audio(void *ctx, const uint8_t *data, size_t n)
{
    if (!track_accepted || track_dead) return false;
    return xStreamBufferSend(audio_in, data, n, pdMS_TO_TICKS(AUDIO_STALL_MS)) == n;
}

static void on_track_end(void *ctx, bool ok)
{
    if (!ok) ESP_LOGW(uart_tag, "Track ended early (player gave up or fell behind)");
}

static const demux_cb_t DEMUX_CB = { on_line, on_track, on_audio, on_track_end };

static void link_rx_task(void *arg)
{
    static demux_t d;
    static uint8_t chunk[512];
    demux_init(&d);
    TickType_t last_data = xTaskGetTickCount();

    for (;;) {
        int got = uart_read_bytes(LINK_UART, chunk, sizeof(chunk), pdMS_TO_TICKS(10));
        if (got > 0) {
            demux_feed(&d, &DEMUX_CB, NULL, chunk, (size_t)got);
            last_data = xTaskGetTickCount();
        } else if (d.state != DEMUX_TEXT && (xTaskGetTickCount() - last_data) > pdMS_TO_TICKS(LINK_IDLE_MS)) {
            ESP_LOGW(uart_tag, "Sender went quiet mid-stream: resyncing");
            demux_abort(&d, &DEMUX_CB, NULL);
            last_data = xTaskGetTickCount();
        }
    }
}

/* ---- USB console: type commands in `pio device monitor` ---------------------------- */

static void usb_init(void)
{
    ESP_ERROR_CHECK(uart_driver_install(USB_UART, 1024, 0, 0, NULL, 0));
}

static void usb_cmd_task(void *arg)
{
    static char line[USB_LINE_MAX];
    size_t len = 0;
    bool overflow = false;
    uint8_t chunk[64];
    for (;;) {
        int got = uart_read_bytes(USB_UART, chunk, sizeof(chunk), pdMS_TO_TICKS(10));
        for (int i = 0; i < got; i++) {
            char c = (char)chunk[i];
            if (c == '\n' || c == '\r') {
                if (!overflow && len > 0) { line[len] = '\0'; servos_handle_line(line); }
                len = 0;
                overflow = false;
            } else if (len < USB_LINE_MAX - 1) {
                line[len++] = c;
            } else {
                overflow = true;
            }
        }
    }
}

/* ---- Decoder glue ------------------------------------------------------------------ */

static int link_read(void *ctx, uint8_t *dst, size_t max, uint32_t timeout_ms)
{
    return (int)xStreamBufferReceive(audio_in, dst, max, pdMS_TO_TICKS(timeout_ms));
}

/* Blocks when the stream buffer is full. That is what paces the whole chain to the DAC. */
static void pcm_write(void *ctx, const uint8_t *pcm, size_t n)
{
    xStreamBufferSend(pcm_stream, pcm, n, portMAX_DELAY);
}

static void player_task(void *arg)
{
    const mp3_pipe_cfg_t pipe = { .read = link_read, .write = pcm_write, .ctx = NULL };

    for (;;) {
        uint32_t len;
        xQueueReceive(track_q, &len, portMAX_DELAY);

        s_flush = false;
        mp3_pipe_result_t r = mp3_pipe_play(&pipe, len);

        if (r.hz != 0 && r.hz != DAC_FREQ_HZ) {
            ESP_LOGW(dac_tag, "MP3 is %d Hz but DAC runs at %d Hz: playback speed will be wrong!", r.hz, DAC_FREQ_HZ);
        }
        if (r.aborted) {
            ESP_LOGW(uart_tag, "Sender stalled, track aborted");
            track_dead = true;                 /* link_rx discards whatever of this track is still coming */
            uint8_t drop[256];
            while (xStreamBufferReceive(audio_in, drop, sizeof(drop), 0) > 0) { }   /* unblock link_rx */
        }
        ESP_LOGI(dac_tag, "Decoded %u samples (%d Hz, %d ch)", (unsigned)r.pcm_bytes, r.hz, r.channels);

        /* Let the last PCM play out, then one DMA ring's worth more */
        s_flush = true;
        while (!xStreamBufferIsEmpty(pcm_stream)) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        vTaskDelay(pdMS_TO_TICKS(DMA_TAIL_MS));
        s_flush = false;
        s_prefilled = false;

        const uint8_t ack = ACK_BYTE;
        tx_enqueue(&ack, 1, false);            /* tell the Pi the clip has finished */
        ESP_LOGI(uart_tag, "Track finished, sent ACK");
    }
}

/* ---- Main ------------------------------------------------------------------------------ */

static void start(TaskFunction_t fn, const char *name, uint32_t stack, UBaseType_t prio, BaseType_t core, TaskHandle_t *h)
{
    if (xTaskCreatePinnedToCore(fn, name, stack, NULL, prio, h, core) != pdPASS) {
        ESP_LOGE(uart_tag, "could not create task '%s'", name);
        abort();   /* a missing task means a deaf or mute board: fail loudly at boot */
    }
}

void app_main(void)
{
    tx_q     = xQueueCreate(24, sizeof(tx_msg_t));
    track_q  = xQueueCreate(1, sizeof(uint32_t));
    audio_in = xStreamBufferCreate(AUDIO_IN_SIZE, 1);
    configASSERT(tx_q && track_q && audio_in);

    dac_init();
    link_init();
    usb_init();
    servos_start();

    start(tx_task,      "tx",      3072,  3, 0, &h_tx);
    start(link_rx_task, "link_rx", 4096,  6, 0, &h_rx);
    start(usb_cmd_task, "usb_cmd", 4096,  4, 0, &h_usb);
    /* minimp3 needs ~20 KB of stack, hence the big one; pinned to core 1 so decoding never competes
     * with the UART/DAC interrupt handling on core 0. */
    start(player_task,  "player",  24 * 1024, 5, 1, &h_player);

    const char *banner = "Tubi ready (one board: audio + servos). Commands: status test run bin head arm door talk dispense stop tasks\n";
    tubi_link_send_text(banner, strlen(banner));
}
