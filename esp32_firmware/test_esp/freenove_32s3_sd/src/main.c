/*
 * ESP32-S3 (Freenove N8R8) : SD card (SDMMC 1-bit) --> UART1 --> ESP32 DevKit V1
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
#define TRACK_PATH    MOUNT_POINT "/CALC.mp3"
 
/* ---- UART link to the ESP32 ---- */
#define LINK_UART     UART_NUM_1
#define LINK_TX_GPIO  1
#define LINK_RX_GPIO  2
#define LINK_CTS_GPIO 14        /* wired to ESP32 RTS (GPIO18) */
#define LINK_BAUD     921600
#define CHUNK_SIZE    4096
 
static esp_err_t sd_mount(void)
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
 
static bool send_track(const char *path)
{
    struct stat st;
    if (stat(path, &st) != 0) {
        ESP_LOGE(TAG, "File not found: %s", path);
        return false;
    }
    FILE *f = fopen(path, "rb");
    if (!f) {
        ESP_LOGE(TAG, "Cannot open %s", path);
        return false;
    }
 
    uint32_t len = (uint32_t)st.st_size;
    uint8_t hdr[7] = { 'A', 'U', 'D',
                       (uint8_t)(len), (uint8_t)(len >> 8),
                       (uint8_t)(len >> 16), (uint8_t)(len >> 24) };
    ESP_LOGI(TAG, "Sending %s (%u bytes)", path, (unsigned)len);
 
    /* Drop any stale back-channel bytes from a previous track */
    uart_flush_input(LINK_UART);
    uart_write_bytes(LINK_UART, hdr, sizeof(hdr));
 
    static uint8_t buf[CHUNK_SIZE];
    size_t n;
    uint32_t sent = 0;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) {
        /* Blocks while the ESP32 holds RTS high (its buffer is full) */
        uart_write_bytes(LINK_UART, buf, n);
        sent += n;
    }
    fclose(f);
    uart_wait_tx_done(LINK_UART, pdMS_TO_TICKS(1000));
    ESP_LOGI(TAG, "Sent %u bytes", (unsigned)sent);
    return true;
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
 
void app_main(void)
{
    if (sd_mount() != ESP_OK) {
        ESP_LOGE(TAG, "SD card mount failed");
        return;
    }
    link_init();
 
    /* Give the ESP32 time to boot and bring up its UART and DAC */
    vTaskDelay(pdMS_TO_TICKS(1500));
 
    while (1) {   /* replays the track forever; replace with a playlist later */
        if (send_track(TRACK_PATH)) {
            if (wait_for_done(10 * 60 * 1000)) {
                ESP_LOGI(TAG, "ESP32 reports track finished");
            } else {
                ESP_LOGW(TAG, "No DONE byte from ESP32 (timeout)");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
