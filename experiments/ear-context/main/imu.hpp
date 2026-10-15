#pragma once
#include <cstdint>
struct ImuSample {
    int64_t t_us = 0;
    float accel[3]{}; // m/s^2, sensor axes, includes gravity
    float gyro[3]{};  // degrees/s, sensor axes
    uint32_t sensor_time = 0;
};
bool imu_init();
bool imu_read(ImuSample& sample);
