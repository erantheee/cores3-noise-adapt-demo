#pragma once
#include <cstdint>
void ui_create();
void ui_update(int cls,float score,float db,bool mic,bool imu,float gyro,int64_t us,uint32_t drops);
