idf_component_register(
    SRCS
        "app_main.cpp"
        "controller.cpp"
        "conversation_awareness.cpp"
        "bone_voice.cpp"
        "imu.cpp"
        "latency_profiler.cpp"
        "mic_capture.cpp"
        "mic_decimator.cpp"
        # voiceprint_gate.cpp 刻意不进固件：端侧没有 WavLM 模型，判定由 Mac 经串口回传
        # self/other（D28）。该模块仍由 test/CMakeLists.txt 编进主机单测。
        "demo_ui.cpp"
        "lv_font_ui_16.c"
    INCLUDE_DIRS "."
)
