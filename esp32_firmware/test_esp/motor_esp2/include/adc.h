#include <stdio.h>
#include "esp_adc/adc_oneshot.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *ADCTAG = "ADC";
/* ---- ADC configuration ------------------------------------------------ */

#define ADC_UNIT            ADC_UNIT_1
#define ADC_CHANNEL         ADC_CHANNEL_0     /* GPIO36 on the ESP32 */
#define ADC_ATTEN           ADC_ATTEN_DB_12
#define ADC_BITWIDTH_CFG    ADC_BITWIDTH_DEFAULT   /* 12 bit: 0-4095 */

#define ADC_LOW_THRESHOLD   1600              /* below  -> min pulse */
#define ADC_HIGH_THRESHOLD  3200              /* above  -> max pulse */

//this might be the error handle???
static adc_oneshot_unit_handle_t adc1_handle;

/* ---- ADC -------------------------------------------------------------- */

static void adc_init(void)
{
    adc_oneshot_unit_init_cfg_t unit_config = {
        .unit_id  = ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit_config, &adc1_handle));
    ESP_LOGI(ADCTAG, "ADC1 initialized, handle: %p", (void *)adc1_handle);

    adc_oneshot_chan_cfg_t chan_config = {
        .atten    = ADC_ATTEN,
        .bitwidth = ADC_BITWIDTH_CFG,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc1_handle, ADC_CHANNEL, &chan_config));
    ESP_LOGI(ADCTAG, "ADC1 channel %d configured", ADC_CHANNEL);
}

