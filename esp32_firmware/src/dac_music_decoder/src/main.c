// /*
//  * ESP32 DOIT DevKit V1 : UART2 (from the S3) --> MP3 decode --> internal DAC (GPIO25) --> LM386
//  * ESP-IDF 5.x
//  *
//  * Stream format (S3 -> ESP32):  'A' 'U' 'D' | uint32 length (little-endian) | MP3 bytes ...
//  * Back-channel (ESP32 -> S3):   'D' = track finished playing
//  *
//  * Data path:
//  *   UART2 RX ring buffer (32 KB, hardware RTS flow control to the S3)
//  *     -> player_task: minimp3 decode, 16-bit -> 8-bit unsigned mono
//  *       -> pcm_stream (16 KB stream buffer)
//  *         -> dac_feed_task: refills the DAC's DMA buffers
//  *           -> GPIO25
//  */
// #include <stdio.h>
// #include <string.h>
// #include "freertos/FreeRTOS.h"
// #include "freertos/task.h"
// #include "freertos/queue.h"
// #include "freertos/stream_buffer.h"
// #include "driver/uart.h"
// #include "driver/dac_continuous.h"
// #include "esp_log.h"
// #include "mp3_pipe.h"

// static const char *uart_tag = "UART";
// static const char *dac_tag  = "DAC";

// /* ---- UART link from the S3 (UART0 stays free for logging/flashing) ---- */
// #define LINK_UART      UART_NUM_2
// #define LINK_RX_GPIO   16          /* <- S3 GPIO1 (TX)  */
// #define LINK_TX_GPIO   17          /* -> S3 GPIO2 (RX)  */
// #define LINK_RTS_GPIO  18          /* -> S3 GPIO14 (CTS) */
// #define LINK_BAUD      921600
// #define LINK_RX_BUF    (32 * 1024) /* ~2.7 s of audio at 96 kbps */

// /* ---- DAC parameters for AUDIO ---- */
// #define DAC_FREQ_HZ    22050       /* must match the MP3's sample rate */
// #define DAC_DESC_NUM   6           /* docs suggest more than 5 so the DMA never starves */
// #define DAC_BUF_SIZE   2048

// #define PCM_STREAM_SIZE (16 * 1024)   /* 8-bit samples: ~0.74 s at 22.05 kHz */
// #define PREFILL_BYTES   (8 * 1024)    /* start playing once this much PCM is queued */
// #define DMA_TAIL_MS     ((DAC_DESC_NUM * DAC_BUF_SIZE * 1000) / DAC_FREQ_HZ + 50)

// static dac_continuous_handle_t asynch_continous = NULL;
// static QueueHandle_t dac_evt_queue = NULL;
// static StreamBufferHandle_t pcm_stream = NULL;   /* decoder writes 8-bit unsigned PCM here (128 = silence) */

// static volatile bool s_prefilled = false;   /* true while the DAC is draining the stream */
// static volatile bool s_flush     = false;   /* true at end of track: play out whatever is left */

// /* ---- DAC ------------------------------------------------------------- */

// // ISR: runs when the DMA finishes one buffer. Just forward the event.
// static bool IRAM_ATTR dac_on_convert_done(dac_continuous_handle_t handle,
//                                           const dac_event_data_t *event,
//                                           void *user_data)
// {
//     QueueHandle_t q = (QueueHandle_t)user_data;
//     BaseType_t woken = pdFALSE;
//     if (xQueueIsQueueFullFromISR(q)) {          // drop oldest if we fell behind
//         dac_event_data_t dummy;
//         xQueueReceiveFromISR(q, &dummy, &woken);
//     }
//     xQueueSendFromISR(q, event, &woken);
//     return woken == pdTRUE;
// }

// // Task: refill each finished DMA buffer with PCM (or silence).
// // NOTE: on the original ESP32 the driver expands 8-bit samples to 16-bit, so a DMA buffer
// // holds only buf_size/2 samples. dac_continuous_write_asynchronously() reports how many
// // bytes it really took in `loaded`; whatever it didn't take must be kept for the next
// // buffer, otherwise audio is silently dropped and the song plays too fast.
// static void dac_feed_task(void *arg)
// {
//     static uint8_t stage[DAC_BUF_SIZE];     // real PCM first, silence padding after it
//     static uint8_t silence[DAC_BUF_SIZE];
//     memset(silence, 128, sizeof(silence));  // 128 = mid-scale silence
//     size_t staged = 0;                      // real PCM bytes waiting in stage[]
//     dac_event_data_t evt;

//     while (1) {
//         xQueueReceive(dac_evt_queue, &evt, portMAX_DELAY);

//         /* Don't start (or restart after an underrun) until there's a cushion of PCM,
//          * unless the track is over and we just need to play out the tail. */
//         if (!s_prefilled &&
//             (s_flush || staged + xStreamBufferBytesAvailable(pcm_stream) >= PREFILL_BYTES)) {
//             s_prefilled = true;
//         }

//         size_t loaded = 0;
//         if (s_prefilled) {
//             staged += xStreamBufferReceive(pcm_stream, stage + staged, DAC_BUF_SIZE - staged, 0);
//             memset(stage + staged, 128, DAC_BUF_SIZE - staged);          // pad with silence
//             dac_continuous_write_asynchronously(asynch_continous, evt.buf, evt.buf_size,
//                                                 stage, DAC_BUF_SIZE, &loaded);
//             if (loaded > staged) {          // the DMA swallowed padding: we ran dry
//                 loaded = staged;
//                 s_prefilled = false;        // re-buffer before resuming
//             }
//             staged -= loaded;               // keep what the DMA didn't take
//             memmove(stage, stage + loaded, staged);
//         } else {
//             dac_continuous_write_asynchronously(asynch_continous, evt.buf, evt.buf_size,
//                                                 silence, DAC_BUF_SIZE, &loaded);
//         }
//     }
// }

// void dac_init(void)
// {
//     dac_continuous_config_t cfg = {
//         .chan_mask = DAC_CHANNEL_MASK_CH0,       // GPIO25
//         .desc_num  = DAC_DESC_NUM,
//         .buf_size  = DAC_BUF_SIZE,
//         .freq_hz   = DAC_FREQ_HZ,
//         .offset    = 0,
//         .clk_src   = DAC_DIGI_CLK_SRC_DEFAULT,
//         .chan_mode = DAC_CHANNEL_MODE_SIMUL,
//     };
//     ESP_ERROR_CHECK(dac_continuous_new_channels(&cfg, &asynch_continous));

//     dac_evt_queue = xQueueCreate(DAC_DESC_NUM, sizeof(dac_event_data_t));
//     pcm_stream    = xStreamBufferCreate(PCM_STREAM_SIZE, 1);

//     dac_event_callbacks_t cbs = {
//         .on_convert_done = dac_on_convert_done,
//         .on_stop = NULL,
//     };
//     ESP_ERROR_CHECK(dac_continuous_register_event_callback(asynch_continous, &cbs, dac_evt_queue));

//     ESP_ERROR_CHECK(dac_continuous_enable(asynch_continous));
//     ESP_ERROR_CHECK(dac_continuous_start_async_writing(asynch_continous));

//     xTaskCreate(dac_feed_task, "dac_feed", 4096, NULL, 6, NULL);
//     ESP_LOGI(dac_tag, "DAC running at %d Hz", DAC_FREQ_HZ);
// }

// /* ---- UART link ------------------------------------------------------- */

// static void link_init(void)
// {
//     uart_config_t cfg = {
//         .baud_rate  = LINK_BAUD,
//         .data_bits  = UART_DATA_8_BITS,
//         .parity     = UART_PARITY_DISABLE,
//         .stop_bits  = UART_STOP_BITS_1,
//         /* RTS goes high when our RX FIFO nears full, telling the S3 to pause.
//          * The FIFO only backs up when the 32 KB ring buffer is full, which is the
//          * intended back-pressure. */
//         .flow_ctrl  = UART_HW_FLOWCTRL_RTS,
//         .rx_flow_ctrl_thresh = 100,              /* of the 128-byte hardware FIFO */
//         .source_clk = UART_SCLK_DEFAULT,
//     };
//     ESP_ERROR_CHECK(uart_driver_install(LINK_UART, LINK_RX_BUF, 0, 0, NULL, 0));
//     ESP_ERROR_CHECK(uart_param_config(LINK_UART, &cfg));
//     ESP_ERROR_CHECK(uart_set_pin(LINK_UART, LINK_TX_GPIO, LINK_RX_GPIO,
//                                  LINK_RTS_GPIO, UART_PIN_NO_CHANGE));
//     ESP_LOGI(uart_tag, "Link ready: UART2 RX=%d TX=%d RTS=%d @ %d baud",
//              LINK_RX_GPIO, LINK_TX_GPIO, LINK_RTS_GPIO, LINK_BAUD);
// }

// /* Hunt for 'A' 'U' 'D' + 4-byte little-endian length. Blocks until a track is announced. */
// static uint32_t wait_for_header(void)
// {
//     static const uint8_t magic[3] = { 'A', 'U', 'D' };
//     for (;;) {
//         uint8_t b;
//         int state = 0;
//         while (state < 3) {
//             if (uart_read_bytes(LINK_UART, &b, 1, portMAX_DELAY) != 1) continue;
//             state = (b == magic[state]) ? state + 1 : (b == 'A' ? 1 : 0);
//         }
//         uint8_t l[4];
//         if (uart_read_bytes(LINK_UART, l, 4, pdMS_TO_TICKS(1000)) == 4) {
//             uint32_t len = (uint32_t)l[0] | ((uint32_t)l[1] << 8) |
//                            ((uint32_t)l[2] << 16) | ((uint32_t)l[3] << 24);
//             if (len > 0 && len < (64u * 1024 * 1024)) {
//                 return len;
//             }
//         }
//     }
// }

// /* ---- Decoder glue ---------------------------------------------------- */

// static int link_read(void *ctx, uint8_t *dst, size_t max, uint32_t timeout_ms)
// {
//     int n = uart_read_bytes(LINK_UART, dst, max, pdMS_TO_TICKS(timeout_ms));
//     return n < 0 ? 0 : n;
// }

// /* Blocks when the stream buffer is full. That is what paces the whole chain to the DAC. */
// static void pcm_write(void *ctx, const uint8_t *pcm, size_t n)
// {
//     xStreamBufferSend(pcm_stream, pcm, n, portMAX_DELAY);
// }

// static void player_task(void *arg)
// {
//     const mp3_pipe_cfg_t pipe = { .read = link_read, .write = pcm_write, .ctx = NULL };

//     for (;;) {
//         uint32_t len = wait_for_header();
//         ESP_LOGI(uart_tag, "Track announced: %u bytes", (unsigned)len);

//         s_flush = false;
//         mp3_pipe_result_t r = mp3_pipe_play(&pipe, len);

//         if (r.hz != DAC_FREQ_HZ) {
//             ESP_LOGW(dac_tag, "MP3 is %d Hz but DAC runs at %d Hz: playback speed will be wrong!",
//                      r.hz, DAC_FREQ_HZ);
//         }
//         if (r.aborted) {
//             ESP_LOGW(uart_tag, "Sender stalled, track aborted");
//             uart_flush_input(LINK_UART);
//         }
//         ESP_LOGI(dac_tag, "Decoded %u samples (%d Hz, %d ch)",
//                  (unsigned)r.pcm_bytes, r.hz, r.channels);

//         /* Let the last PCM play out, then one DMA ring's worth more */
//         s_flush = true;
//         while (!xStreamBufferIsEmpty(pcm_stream)) {
//             vTaskDelay(pdMS_TO_TICKS(20));
//         }
//         vTaskDelay(pdMS_TO_TICKS(DMA_TAIL_MS));
//         s_flush = false;
//         s_prefilled = false;

//         uart_write_bytes(LINK_UART, "D", 1);     /* tell the S3 we're done */
//         ESP_LOGI(uart_tag, "Track finished, sent DONE");
//     }
// }

// /* ---- Main ------------------------------------------------------------- */

// void app_main(void)
// {
//     dac_init();
//     link_init();
//     /* minimp3 needs ~20 KB of stack, hence the big one; pinned to core 1 so decoding
//      * never competes with the UART/DAC interrupt handling on core 0. */
//     xTaskCreatePinnedToCore(player_task, "player", 24 * 1024, NULL, 5, NULL, 1);
// }

/*
 * ESP32 DOIT DevKit V1 : UART2 (from the S3) --> MP3 decode --> internal DAC (GPIO25) --> LM386
 * ESP-IDF 5.x
 *
 * Stream format (S3 -> ESP32):  'A' 'U' 'D' | uint32 length (little-endian) | MP3 bytes ...
 * Back-channel (ESP32 -> S3):   'D' = track finished playing
 *
 * Data path:
 *   UART2 RX ring buffer (32 KB, hardware RTS flow control to the S3)
 *     -> player_task: minimp3 decode, 16-bit -> 8-bit unsigned mono
 *       -> pcm_stream (16 KB stream buffer)
 *         -> dac_feed_task: refills the DAC's DMA buffers
 *           -> GPIO25
 */
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/stream_buffer.h"
#include "driver/uart.h"
#include "driver/dac_continuous.h"
#include "esp_log.h"
#include "mp3_pipe.h"

static const char *uart_tag = "UART";
static const char *dac_tag  = "DAC";

/* ---- UART link from the S3 (UART0 stays free for logging/flashing) ---- */
#define LINK_UART      UART_NUM_2
#define LINK_RX_GPIO   16          /* <- S3 GPIO1 (TX)  */
#define LINK_TX_GPIO   17          /* -> S3 GPIO2 (RX)  */
#define LINK_RTS_GPIO  18          /* -> S3 GPIO14 (CTS) */
#define LINK_BAUD      921600
#define LINK_RX_BUF    (32 * 1024) /* ~2.7 s of audio at 96 kbps */

/* ---- DAC parameters for AUDIO ---- */
#define DAC_FREQ_HZ    22050       /* must match the MP3's sample rate */
#define DAC_DESC_NUM   6           /* docs suggest more than 5 so the DMA never starves */
#define DAC_BUF_SIZE   2048

#define PCM_STREAM_SIZE (16 * 1024)   /* 8-bit samples: ~0.74 s at 22.05 kHz */
#define PREFILL_BYTES   (8 * 1024)    /* start playing once this much PCM is queued */
#define DMA_TAIL_MS     ((DAC_DESC_NUM * DAC_BUF_SIZE * 1000) / DAC_FREQ_HZ + 50)

static dac_continuous_handle_t asynch_continous = NULL;
static QueueHandle_t dac_evt_queue = NULL;
static StreamBufferHandle_t pcm_stream = NULL;   /* decoder writes 8-bit unsigned PCM here (128 = silence) */

static volatile bool s_prefilled = false;   /* true while the DAC is draining the stream */
static volatile bool s_flush     = false;   /* true at end of track: play out whatever is left */

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

/* ---- UART link ------------------------------------------------------- */

static void link_init(void)
{
    uart_config_t cfg = {
        .baud_rate  = LINK_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        /* RTS goes high when our RX FIFO nears full, telling the S3 to pause.
         * The FIFO only backs up when the 32 KB ring buffer is full, which is the
         * intended back-pressure. */
        .flow_ctrl  = UART_HW_FLOWCTRL_RTS,
        .rx_flow_ctrl_thresh = 100,              /* of the 128-byte hardware FIFO */
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(LINK_UART, LINK_RX_BUF, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(LINK_UART, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(LINK_UART, LINK_TX_GPIO, LINK_RX_GPIO,
                                 LINK_RTS_GPIO, UART_PIN_NO_CHANGE));
    ESP_LOGI(uart_tag, "Link ready: UART2 RX=%d TX=%d RTS=%d @ %d baud",
             LINK_RX_GPIO, LINK_TX_GPIO, LINK_RTS_GPIO, LINK_BAUD);
}

/* Hunt for 'A' 'U' 'D' + 4-byte little-endian length. Blocks until a track is announced. */
static uint32_t wait_for_header(void)
{
    static const uint8_t magic[3] = { 'A', 'U', 'D' };
    for (;;) {
        uint8_t b;
        int state = 0;
        while (state < 3) {
            if (uart_read_bytes(LINK_UART, &b, 1, portMAX_DELAY) != 1) continue;
            state = (b == magic[state]) ? state + 1 : (b == 'A' ? 1 : 0);
        }
        uint8_t l[4];
        if (uart_read_bytes(LINK_UART, l, 4, pdMS_TO_TICKS(1000)) == 4) {
            uint32_t len = (uint32_t)l[0] | ((uint32_t)l[1] << 8) |
                           ((uint32_t)l[2] << 16) | ((uint32_t)l[3] << 24);
            if (len > 0 && len < (64u * 1024 * 1024)) {
                return len;
            }
        }
    }
}

/* ---- Decoder glue ---------------------------------------------------- */

static int link_read(void *ctx, uint8_t *dst, size_t max, uint32_t timeout_ms)
{
    int n = uart_read_bytes(LINK_UART, dst, max, pdMS_TO_TICKS(timeout_ms));
    return n < 0 ? 0 : n;
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
        uint32_t len = wait_for_header();
        ESP_LOGI(uart_tag, "Track announced: %u bytes", (unsigned)len);

        s_flush = false;
        mp3_pipe_result_t r = mp3_pipe_play(&pipe, len);

        if (r.hz != DAC_FREQ_HZ) {
            ESP_LOGW(dac_tag, "MP3 is %d Hz but DAC runs at %d Hz: playback speed will be wrong!",
                     r.hz, DAC_FREQ_HZ);
        }
        if (r.aborted) {
            ESP_LOGW(uart_tag, "Sender stalled, track aborted");
            uart_flush_input(LINK_UART);
        }
        ESP_LOGI(dac_tag, "Decoded %u samples (%d Hz, %d ch)",
                 (unsigned)r.pcm_bytes, r.hz, r.channels);

        /* Let the last PCM play out, then one DMA ring's worth more */
        s_flush = true;
        while (!xStreamBufferIsEmpty(pcm_stream)) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        vTaskDelay(pdMS_TO_TICKS(DMA_TAIL_MS));
        s_flush = false;
        s_prefilled = false;

        uart_write_bytes(LINK_UART, "D", 1);     /* tell the S3 we're done */
        ESP_LOGI(uart_tag, "Track finished, sent DONE");
    }
}

/* ---- Main ------------------------------------------------------------- */

void app_main(void)
{
    dac_init();
    link_init();
    /* minimp3 needs ~20 KB of stack, hence the big one; pinned to core 1 so decoding
     * never competes with the UART/DAC interrupt handling on core 0. */
    xTaskCreatePinnedToCore(player_task, "player", 24 * 1024, NULL, 5, NULL, 1);
}