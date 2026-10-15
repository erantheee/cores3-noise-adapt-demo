idf_component_register(SRCS "app_main.cpp" "imu.cpp" "ui.cpp" "ear_font16.c" "ear_font24.c" "../../../main/controller.cpp"
  INCLUDE_DIRS "." "../../../main" "../../model-bench/main"
  REQUIRES bmi270 m5stack_core_s3 esp_timer)
