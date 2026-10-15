#include <cstdio>
#include <cstring>
#include <cmath>
#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "lvgl.h"
#include "imu.hpp"
#include "ui.hpp"
#include "window.hpp"
#include "listening_model.h"


static QueueHandle_t imu_queue;
static float gyro_speed=0;
static portMUX_TYPE state_lock = portMUX_INITIALIZER_UNLOCKED;
static uint32_t imu_drops=0;
static int64_t imu_last_us=0;
static bool imu_ok=false;
struct Telemetry {
    AnalysisFrame f;
    float features[92];
    int cls;
    float score;
    int64_t now, inference_us;
    uint32_t seq, audio_errors, drops, context_drops;
    bool ok;
};
static QueueHandle_t context_queue;
static void log_task(void*) {
    while(true) {
        ImuSample s;
        for(int i=0;i<32 && xQueueReceive(imu_queue,&s,0)==pdTRUE;++i)
            printf("{\"type\":\"imu\",\"t_us\":%lld,\"sensor_ticks\":%lu,\"accel_mps2\":[%.6f,%.6f,%.6f],\"gyro_dps\":[%.6f,%.6f,%.6f]}\n",
                (long long)s.t_us,(unsigned long)s.sensor_time,s.accel[0],s.accel[1],s.accel[2],s.gyro[0],s.gyro[1],s.gyro[2]);
        Telemetry p;
        if(xQueueReceive(context_queue,&p,0)==pdTRUE) {
            const auto& f=p.f;
            const char* name=p.cls>=0?listening_model::kClassNames[p.cls]:"unknown";
            printf("{\"type\":\"context\",\"schema\":1,\"seq\":%lu,\"t_us\":%lld,\"class\":\"%s\",\"score\":%.5f,\"mic_valid\":%s,\"level_dbfs\":%.5f,\"voice\":%.5f,\"imu_valid\":%s,\"imu_drops\":%lu,\"context_drops\":%lu,\"audio_errors\":%lu,\"clock_valid\":false,\"activity\":\"untrained\",\"earbud_connected\":false,\"inference_us\":%lld,\"bands\":[",
                (unsigned long)p.seq,(long long)p.now,name,p.score,f.mic_valid?"true":"false",f.broadband_dbfs,f.voice_likelihood,p.ok?"true":"false",(unsigned long)p.drops,(unsigned long)p.context_drops,(unsigned long)p.audio_errors,(long long)p.inference_us);
            for(int i=0;i<24;++i) printf("%s%.6f",i?",":"",f.band_dbfs[i]);
            printf("],\"features\":");
            if(p.cls<0) printf("null");
            else { printf("["); for(int i=0;i<92;++i) printf("%s%.8g",i?",":"",p.features[i]); printf("]"); }
            printf("}\n");
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

static void imu_task(void*) {
    bool ok=imu_init();
    taskENTER_CRITICAL(&state_lock); imu_ok=ok; taskEXIT_CRITICAL(&state_lock);
    if (!ok) { vTaskDelete(nullptr); return; }
    TickType_t tick=xTaskGetTickCount();
    while(true) {
        ImuSample s;
        if(imu_read(s)) {
            bool dropped=xQueueSend(imu_queue,&s,0)!=pdTRUE;
            taskENTER_CRITICAL(&state_lock);
            imu_last_us=s.t_us; imu_drops+=dropped;
            gyro_speed=std::sqrt(s.gyro[0]*s.gyro[0]+s.gyro[1]*s.gyro[1]+s.gyro[2]*s.gyro[2]);
            taskEXIT_CRITICAL(&state_lock);
        }
        vTaskDelayUntil(&tick,pdMS_TO_TICKS(5));
    }
}

static void audio_task(void*) {
    i2s_std_config_t cfg = {
        .clk_cfg=I2S_STD_CLK_DEFAULT_CONFIG(48000),
        .slot_cfg=I2S_STD_PHILIP_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,I2S_SLOT_MODE_MONO),
        .gpio_cfg={.mclk=BSP_I2S_MCLK,.bclk=BSP_I2S_SCLK,.ws=BSP_I2S_LCLK,
                   .dout=BSP_I2S_DOUT,.din=BSP_I2S_DSIN,.invert_flags={}},
    };
    ESP_ERROR_CHECK(bsp_audio_init(&cfg));
    auto mic=bsp_audio_codec_microphone_init();
    ESP_ERROR_CHECK(mic ? ESP_OK : ESP_FAIL);
    esp_codec_dev_sample_info_t format{};
    format.sample_rate=48000; format.channel=1; format.bits_per_sample=16;
    ESP_ERROR_CHECK(esp_codec_dev_set_in_gain(mic,36.f));
    ESP_ERROR_CHECK(esp_codec_dev_open(mic,&format));
    static int16_t ring[2560],block[480];
    static Window window;
    NoiseAnalyzer analyzer;
    int filled=0,frames=0,blocks=0;
    uint32_t audio_errors=0,seq=0,context_drops=0;
    int64_t last_frame=0;
    while(true) {
        if(esp_codec_dev_read(mic,block,sizeof(block))!=ESP_OK) {
            ++audio_errors; filled=frames=blocks=0;
            if(bsp_display_lock(20)) { ui_update(-1,0,-120,false,false,0,0,0); bsp_display_unlock(); }
            vTaskDelay(pdMS_TO_TICKS(10)); continue;
        }
        int drop=std::max(0,filled+480-2560);
        memmove(ring,ring+drop,(filled-drop)*sizeof(int16_t)); filled-=drop;
        memcpy(ring+filled,block,sizeof(block)); filled+=480;
        if(++blocks<10) continue;
        blocks=0;
        int64_t now=esp_timer_get_time();
        if(last_frame && now-last_frame>250000) frames=0;
        last_frame=now;
        auto f=analyzer.analyze(ring,filled,now/1000);
        if(frames==20) { for(int i=1;i<20;++i) window[i-1]=window[i]; --frames; }
        window[frames++]={f,extract_voice_features(f),now/1e6};
        float features[92]{},logits[9],score=0;
        int cls=-1; int64_t inference_us=0;
        if(frames==20 && f.mic_valid) {
            build_window(window,features);
            auto start=esp_timer_get_time(); listening_model::classify(features,logits);
            inference_us=esp_timer_get_time()-start;
            cls=0; for(int i=1;i<9;++i) if(logits[i]>logits[cls]) cls=i;
            double sum=0; for(float x:logits) sum+=std::exp(x-logits[cls]); score=1/sum;
        }
        if(!f.mic_valid) frames=0;
        bool ok; uint32_t drops; int64_t imu_time; float gyro;
        taskENTER_CRITICAL(&state_lock);
        ok=imu_ok; drops=imu_drops; imu_time=imu_last_us; gyro=gyro_speed;
        taskEXIT_CRITICAL(&state_lock);
        ok=ok && imu_time>0 && now-imu_time<500000;
        Telemetry packet{};
        packet.f=f; memcpy(packet.features,features,sizeof(features));
        packet.cls=cls; packet.score=score; packet.now=now; packet.inference_us=inference_us;
        packet.seq=seq++; packet.audio_errors=audio_errors; packet.drops=drops;
        packet.context_drops=context_drops; packet.ok=ok;
        if(xQueueSend(context_queue,&packet,0)!=pdTRUE) ++context_drops;
        if(seq%5==0 && bsp_display_lock(20)) {
            ui_update(cls,score,f.broadband_dbfs,f.mic_valid,ok,gyro,inference_us,drops+context_drops);
            bsp_display_unlock();
        }
        // Yield even if DMA has accumulated a backlog.
        vTaskDelay(1);
    }
}
extern "C" void app_main() {
    bsp_display_start();
    bsp_display_lock(0);
    ui_create();
    bsp_display_unlock(); bsp_display_backlight_on();
    imu_queue=xQueueCreate(128,sizeof(ImuSample));
    context_queue=xQueueCreate(4,sizeof(Telemetry));
    configASSERT(imu_queue && context_queue);
    xTaskCreatePinnedToCore(log_task,"telemetry",6144,nullptr,3,nullptr,0);
    xTaskCreatePinnedToCore(imu_task,"imu",6144,nullptr,5,nullptr,0);
    xTaskCreatePinnedToCore(audio_task,"context",16384,nullptr,5,nullptr,1);
}
