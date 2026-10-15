idf_component_register(SRCS "app_main.cpp" "imu.cpp" "../../../main/controller.cpp" "../../../main/lv_font_ui_16.c"
  INCLUDE_DIRS "." "../../../main" "../../model-bench/main"
  REQUIRES bmi270 m5stack_core_s3 esp_timer)
