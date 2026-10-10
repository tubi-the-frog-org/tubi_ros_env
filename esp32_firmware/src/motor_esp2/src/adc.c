#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_adc/adc_oneshot.h"
#include "hal/adc_types.h"
#include <driver/gpio.h>
#define ADC_UNIT     ADC_UNIT_1
#define ADC_CHANNEL  ADC_CHANNEL_0
#define ADC_ATTEN    ADC_ATTEN_DB_12
#define ADC_BITWDITH ADC_BITWIDTH_DEFAULT    
static const char *TAG = "ADC_READ";
static adc_oneshot_unit_handle_t adc1_handle;
static void adc_init(void){
    //**adc init**
    adc_oneshot_unit_init_cfg_t init_config1 = {
        .unit_id = ADC_UNIT,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_config1,&adc1_handle)); //includes handle and config struct
    ESP_LOGI(TAG, "ADC1 initialized, handle: %p",adc1_handle);

    //* * adc 1 channel config * *
    adc_oneshot_chan_cfg_t config_channel = {
        .atten = ADC_ATTEN,
        .bitwidth = ADC_BITWDITH,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc1_handle, ADC_CHANNEL, &config_channel));
    ESP_LOGI(TAG, "ADC1 Channel %d confguired, handle: %d",ADC_CHANNEL);
}

void app_main(void){
    adc_init();
    int adc_raw_reading;
    while(1){
        esp_err_t ret =adc_oneshot_read(adc1_handle,ADC_CHANNEL, &adc_raw_reading);
        if(ret == ESP_OK){
            ESP_LOGI(TAG,"ADC1 Channel[%d] Raw Data: %d", ADC_CHANNEL,adc_raw_reading);
        }else if(ret==ESP_ERR_TIMEOUT){
            ESP_LOGW(TAG,"ADC Read Timeout");
        }else{
            ESP_LOGE(TAG,"ADC read fialed with error: %s", esp_err_to_name(ret));
        }
    vTaskDelay(pdMS_TO_TICKS(15));
    }
}