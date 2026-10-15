#include "imu.hpp"
#include <cstring>
#include "bmi270.h"
#include "bsp/esp-bsp.h"
#include "driver/i2c_master.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static i2c_master_dev_handle_t handle;
static bmi2_dev device{};
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
    if (bsp_i2c_init() != ESP_OK) return false;
    i2c_master_bus_handle_t bus;
    if (i2c_master_get_bus_handle(BSP_I2C_NUM, &bus) != ESP_OK) return false;
    i2c_device_config_t config{};
    config.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    config.device_address = 0x68;
    config.scl_speed_hz = 400000;
    if (i2c_master_bus_add_device(bus, &config, &handle) != ESP_OK) return false;
    device.intf = BMI2_I2C_INTF;
    device.read = read_reg;
    device.write = write_reg;
    device.delay_us = delay_us;
    device.read_write_len = 32;
    if (bmi270_init(&device) != BMI2_OK) return false;
    bmi2_sens_config cfg[2]{};
    cfg[0].type = BMI2_ACCEL;
    cfg[1].type = BMI2_GYRO;
    if (bmi2_get_sensor_config(cfg, 2, &device) != BMI2_OK) return false;
    cfg[0].cfg.acc.odr = BMI2_ACC_ODR_100HZ;
    cfg[0].cfg.acc.range = BMI2_ACC_RANGE_4G;
    cfg[0].cfg.acc.bwp = BMI2_ACC_NORMAL_AVG4;
    cfg[0].cfg.acc.filter_perf = BMI2_PERF_OPT_MODE;
    cfg[1].cfg.gyr.odr = BMI2_GYR_ODR_100HZ;
    cfg[1].cfg.gyr.range = BMI2_GYR_RANGE_500;
    cfg[1].cfg.gyr.bwp = BMI2_GYR_NORMAL_MODE;
    cfg[1].cfg.gyr.filter_perf = BMI2_PERF_OPT_MODE;
    if (bmi2_set_sensor_config(cfg, 2, &device) != BMI2_OK) return false;
    uint8_t sensors[] = {BMI2_ACCEL, BMI2_GYRO};
    return bmi2_sensor_enable(sensors, 2, &device) == BMI2_OK;
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
