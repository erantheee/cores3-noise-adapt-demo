#include "imu.hpp"
#include <cstring>
#include "bmi270.h"
#include "bsp/esp-bsp.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static i2c_master_dev_handle_t handle;
static bmi2_dev device{};
static const char* kTag = "imu";
static BMI2_INTF_RETURN_TYPE read_reg(uint8_t reg, uint8_t* data, uint32_t len, void*) {
    return i2c_master_transmit_receive(handle, &reg, 1, data, len, 100) == ESP_OK ? 0 : -1;
}
static BMI2_INTF_RETURN_TYPE write_reg(uint8_t reg, const uint8_t* data, uint32_t len, void*) {
    uint8_t buffer[33];
    if (len > 32) return -1;
    buffer[0] = reg;
    memcpy(buffer + 1, data, len);
    return i2c_master_transmit(handle, buffer, len + 1, 100) == ESP_OK ? 0 : -1;
}
static void delay_us(uint32_t us, void*) {
    esp_rom_delay_us(us);
}
bool imu_init() {
    if (bsp_i2c_init() != ESP_OK) { ESP_LOGE(kTag, "bsp_i2c_init failed"); return false; }
    i2c_master_bus_handle_t bus;
    if (i2c_master_get_bus_handle(BSP_I2C_NUM, &bus) != ESP_OK) {
        ESP_LOGE(kTag, "no i2c bus handle for port %d", BSP_I2C_NUM); return false;
    }
    // BMI270 sits at 0x68 or 0x69 depending on the SDO strap; probe instead of assuming.
    uint8_t address = 0;
    const uint8_t candidates[2] = {0x68, 0x69};
    for (int i = 0; i < 2 && address == 0; ++i) {
        if (i2c_master_probe(bus, candidates[i], 100) == ESP_OK) address = candidates[i];
    }
    if (address == 0) { ESP_LOGE(kTag, "no BMI270 found on 0x68/0x69"); return false; }
    i2c_device_config_t config{};
    config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    config.device_address = address;
    config.scl_speed_hz = 400000;
    if (i2c_master_bus_add_device(bus, &config, &handle) != ESP_OK) {
        ESP_LOGE(kTag, "add_device 0x%02x failed", address); return false;
    }
    device.intf = BMI2_I2C_INTF;
    device.read = read_reg;
    device.write = write_reg;
    device.delay_us = delay_us;
    device.read_write_len = 32;
    if (bmi270_init(&device) != BMI2_OK) { ESP_LOGE(kTag, "bmi270_init failed"); return false; }
    bmi2_sens_config cfg[2]{};
    cfg[0].type = BMI2_ACCEL;
    cfg[1].type = BMI2_GYRO;
    if (bmi2_get_sensor_config(cfg, 2, &device) != BMI2_OK) {
        ESP_LOGE(kTag, "get_sensor_config failed"); return false;
    }
    cfg[0].cfg.acc.odr = BMI2_ACC_ODR_100HZ;
    cfg[0].cfg.acc.range = BMI2_ACC_RANGE_4G;
    cfg[0].cfg.acc.bwp = BMI2_ACC_NORMAL_AVG4;
    cfg[0].cfg.acc.filter_perf = BMI2_PERF_OPT_MODE;
    cfg[1].cfg.gyr.odr = BMI2_GYR_ODR_100HZ;
    cfg[1].cfg.gyr.range = BMI2_GYR_RANGE_500;
    cfg[1].cfg.gyr.bwp = BMI2_GYR_NORMAL_MODE;
    cfg[1].cfg.gyr.filter_perf = BMI2_PERF_OPT_MODE;
    if (bmi2_set_sensor_config(cfg, 2, &device) != BMI2_OK) {
        ESP_LOGE(kTag, "set_sensor_config failed"); return false;
    }
    // Without the data-ready interrupt mapping the DRDY bits never appear in the
    // status register, and every poll looks like "no new sample" -- the exact silent
    // failure seen on 2026-10-15. The official example maps it before enabling sensors.
    if (bmi2_map_data_int(BMI2_DRDY_INT, BMI2_INT1, &device) != BMI2_OK) {
        ESP_LOGE(kTag, "map_data_int failed"); return false;
    }
    uint8_t sensors[] = {BMI2_ACCEL, BMI2_GYRO};
    const bool enabled = bmi2_sensor_enable(sensors, 2, &device) == BMI2_OK;
    ESP_LOGI(kTag, "bmi270_init ok, addr=0x%02x accel=4g gyro=500dps odr=100Hz enabled=%d",
             address, enabled ? 1 : 0);
    return enabled;
}
bool imu_read(ImuSample& sample) {
    bmi2_sens_data data{};
    if (bmi2_get_sensor_data(&data, &device) != BMI2_OK ||
        !(data.status & BMI2_DRDY_ACC) || !(data.status & BMI2_DRDY_GYR)) return false;
    sample.t_us = esp_timer_get_time();
    const float a = 4.f * 9.80665f / 32768.f;
    const float g = 500.f / 32768.f;
    sample.accel[0] = data.acc.x * a; sample.accel[1] = data.acc.y * a; sample.accel[2] = data.acc.z * a;
    sample.gyro[0] = data.gyr.x * g; sample.gyro[1] = data.gyr.y * g; sample.gyro[2] = data.gyr.z * g;
    sample.sensor_time = data.sens_time;
    return true;
}
