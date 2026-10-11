/*
 * ESP32-S3 (Freenove N8R8) : Raspberry Pi (UART2) --> UART1 --> ESP32 DevKit V1 (MP3 decode + DAC)
 * Bridge only: tracks come from pi_app (TTS replies), not the SD card. sd_mount() is kept but unused.
 * ESP-IDF 5.x
 *
 * Stream format (S3 -> ESP32):
 *     'A' 'U' 'D' | uint32 length (little-endian) | MP3 bytes ...
 * Back-channel (ESP32 -> S3):
 *     'D' = track finished playing
 *
 * main/CMakeLists.txt:
 *     idf_component_register(SRCS "main.c" INCLUDE_DIRS "." PRIV_REQUIRES driver fatfs sdmmc)
 */
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <sys/stat.h>
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#include "driver/sdmmc_host.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
 
static const char *TAG = "S3_SENDER";
 
/* ---- SD card (Freenove board, 1-bit SDMMC) ---- */
#define SD_CMD_GPIO   38
#define SD_CLK_GPIO   39
#define SD_D0_GPIO    40
#define MOUNT_POINT   "/sdcard"
/* Keep filenames 8.3 (like this one) unless you enable long filename support in menuconfig */
 
/* ---- UART from the Raspberry Pi (pi_app's Esp32AudioPlayer), 3.3 V logic, common GND ---- */
#define PI_UART       UART_NUM_2
#define PI_RX_GPIO    4         /* <- Pi pin 8  (GPIO14 TXD) */
#define PI_TX_GPIO    5         /* -> Pi pin 10 (GPIO15 RXD) */
#define PI_BAUD       921600
#define PI_RX_BUF     (32 * 1024)

/* ---- UART link to the ESP32 ---- */
#define LINK_UART     UART_NUM_1
#define LINK_TX_GPIO  1
#define LINK_RX_GPIO  2
#define LINK_CTS_GPIO 14        /* wired to ESP32 RTS (GPIO18) */
#define LINK_BAUD     921600
#define CHUNK_SIZE    4096
 
static void pi_init(void)
{
    uart_config_t cfg = {
        .baud_rate  = PI_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,   /* the Pi paces itself just above the audio bitrate */
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_driver_install(PI_UART, PI_RX_BUF, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(PI_UART, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(PI_UART, PI_TX_GPIO, PI_RX_GPIO,
                                 UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE));
}

__attribute__((unused)) static esp_err_t sd_mount(void)
{
    esp_vfs_fat_sdmmc_mount_config_t mount_cfg = {
        .format_if_mount_failed = false,
        .max_files = 4,
        .allocation_unit_size = 16 * 1024,
    };
 
    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.flags = SDMMC_HOST_FLAG_1BIT;
 
    sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
    slot.width = 1;
    slot.clk = SD_CLK_GPIO;
    slot.cmd = SD_CMD_GPIO;
    slot.d0  = SD_D0_GPIO;
    slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;
 
    sdmmc_card_t *card = NULL;
    esp_err_t err = esp_vfs_fat_sdmmc_mount(MOUNT_POINT, &host, &slot, &mount_cfg, &card);
    if (err == ESP_OK) {
        sdmmc_card_print_info(stdout, card);
    }
    return err;
}
 
static void link_init(void)
{
    uart_config_t cfg = {
        .baud_rate  = LINK_BAUD,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        /* We only transmit, so we only need CTS (the ESP32 drives it via its RTS).
         * While testing WITHOUT the ESP32 attached, switch this to UART_HW_FLOWCTRL_DISABLE,
         * otherwise a floating CTS pin can stall the transmitter. */
        .flow_ctrl  = UART_HW_FLOWCTRL_CTS,
        .source_clk = UART_SCLK_DEFAULT,
    };
    /* RX buffer for the back-channel, TX buffer 0 so uart_write_bytes() blocks (and respects CTS) */
    ESP_ERROR_CHECK(uart_driver_install(LINK_UART, 1024, 0, 0, NULL, 0));
    ESP_ERROR_CHECK(uart_param_config(LINK_UART, &cfg));
    ESP_ERROR_CHECK(uart_set_pin(LINK_UART, LINK_TX_GPIO, LINK_RX_GPIO,
                                 UART_PIN_NO_CHANGE, LINK_CTS_GPIO));
}
 
/* Waits for the ESP32 to report 'D' (finished playing). */
static bool wait_for_done(uint32_t timeout_ms)
{
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);
    uint8_t b;
    while (xTaskGetTickCount() < deadline) {
        if (uart_read_bytes(LINK_UART, &b, 1, pdMS_TO_TICKS(200)) == 1 && b == 'D') {
            return true;
        }
    }
    return false;
}
 
/* Hunt for 'A' 'U' 'D' + 4-byte little-endian length from the Pi. Blocks until a track is announced. */
static uint32_t wait_for_pi_header(void)
{
    static const uint8_t magic[3] = { 'A', 'U', 'D' };
    for (;;) {
        uint8_t b;
        int state = 0;
        while (state < 3) {
            if (uart_read_bytes(PI_UART, &b, 1, portMAX_DELAY) != 1) continue;
            state = (b == magic[state]) ? state + 1 : (b == 'A' ? 1 : 0);
        }
        uint8_t l[4];
        if (uart_read_bytes(PI_UART, l, 4, pdMS_TO_TICKS(1000)) == 4) {
            uint32_t len = (uint32_t)l[0] | ((uint32_t)l[1] << 8) |
                           ((uint32_t)l[2] << 16) | ((uint32_t)l[3] << 24);
            if (len > 0 && len < (64u * 1024 * 1024)) {
                return len;
            }
        }
    }
}

/* Forward one track Pi -> ESP32. The ESP32's RTS (our CTS) throttles uart_write_bytes() below. */
static bool forward_track(uint32_t len)
{
    uint8_t hdr[7] = { 'A', 'U', 'D',
                       (uint8_t)(len), (uint8_t)(len >> 8),
                       (uint8_t)(len >> 16), (uint8_t)(len >> 24) };
    ESP_LOGI(TAG, "Pi announced %u bytes, forwarding", (unsigned)len);

    uart_flush_input(LINK_UART);                 /* stale back-channel bytes from a previous track */
    uart_write_bytes(LINK_UART, hdr, sizeof(hdr));

    static uint8_t buf[CHUNK_SIZE];
    uint32_t remaining = len;
    int stalls = 0;
    while (remaining > 0) {
        size_t want = remaining < sizeof(buf) ? remaining : sizeof(buf);
        int n = uart_read_bytes(PI_UART, buf, want, pdMS_TO_TICKS(1000));
        if (n <= 0) {
            if (++stalls >= 5) {                 /* 5 s of silence from the Pi: give up */
                ESP_LOGW(TAG, "Pi stalled with %u bytes left, track aborted", (unsigned)remaining);
                return false;
            }
            continue;
        }
        stalls = 0;
        uart_write_bytes(LINK_UART, buf, n);     /* blocks while the ESP32 holds RTS high */
        remaining -= (uint32_t)n;
    }
    uart_wait_tx_done(LINK_UART, pdMS_TO_TICKS(1000));
    return true;
}

void app_main(void)
{
    link_init();
    pi_init();

    /* Give the ESP32 time to boot and bring up its UART and DAC */
    vTaskDelay(pdMS_TO_TICKS(1500));
    ESP_LOGI(TAG, "Bridge ready: Pi (UART%d RX=%d TX=%d) -> ESP32 (UART%d)",
             PI_UART, PI_RX_GPIO, PI_TX_GPIO, LINK_UART);

    while (1) {
        uint32_t len = wait_for_pi_header();
        if (forward_track(len)) {
            /* The DOIT sends 'D' when the clip has finished playing, i.e. about len/4000 s after the first
             * byte at 32 kbit/s. Allow a generous len/2000 s (+15 s) so a lost 'D' can never keep the
             * bridge from accepting the next sentence for minutes. */
            if (wait_for_done(len / 2 + 15000)) {
                ESP_LOGI(TAG, "ESP32 reports track finished, telling the Pi");
                uart_write_bytes(PI_UART, "D", 1);   /* Pi's Esp32AudioPlayer waits for this */
            } else {
                ESP_LOGW(TAG, "No DONE byte from ESP32 (timeout)");
            }
        }
    }
}
