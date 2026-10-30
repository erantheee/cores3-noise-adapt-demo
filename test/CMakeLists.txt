cmake_minimum_required(VERSION 3.16)
project(cores3_host_tests LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The analyser, controller and latency profiler are deliberately free of ESP-IDF
# dependencies so the whole decision path can be built and exercised on a development
# host. Keep it that way: if a target file starts including an IDF header, these tests
# stop being runnable here.
set(COMMON_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/controller.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/latency_profiler.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/mic_capture.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/mic_decimator.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/voiceprint_gate.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/conversation_awareness.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/bone_voice.cpp
)

enable_testing()

add_executable(controller_test controller_test.cpp ${COMMON_SOURCES})
target_include_directories(controller_test PRIVATE ../main)
add_test(NAME controller_test COMMAND controller_test)

add_executable(noise_analyzer_test noise_analyzer_test.cpp ${COMMON_SOURCES})
target_include_directories(noise_analyzer_test PRIVATE ../main)
add_test(NAME noise_analyzer_test COMMAND noise_analyzer_test)

add_executable(latency_profiler_test latency_profiler_test.cpp ${COMMON_SOURCES})
target_include_directories(latency_profiler_test PRIVATE ../main)
add_test(NAME latency_profiler_test COMMAND latency_profiler_test)

add_executable(mic_capture_test mic_capture_test.cpp ${COMMON_SOURCES})
target_include_directories(mic_capture_test PRIVATE ../main)
add_test(NAME mic_capture_test COMMAND mic_capture_test)

add_executable(mic_decimator_test mic_decimator_test.cpp ${COMMON_SOURCES})
target_include_directories(mic_decimator_test PRIVATE ../main)
add_test(NAME mic_decimator_test COMMAND mic_decimator_test)

add_executable(voiceprint_gate_test voiceprint_gate_test.cpp ${COMMON_SOURCES})
target_include_directories(voiceprint_gate_test PRIVATE ../main)
add_test(NAME voiceprint_gate_test COMMAND voiceprint_gate_test)

# The 92-dim model input window. It needs no COMMON_SOURCES: context_window.hpp is header-only
# and deliberately free of ESP-IDF dependencies so this port can be checked against the
# training-side implementation on the host (see test/context_window_golden.h).
add_executable(context_window_test context_window_test.cpp)
target_include_directories(context_window_test PRIVATE ../main)
add_test(NAME context_window_test COMMAND context_window_test)

# Apple 式对话感知（自身语音触发 → 下压媒体 → 保持 → 恢复）。只有它自己的状态机，
# 不依赖分析器：证据由调用方填，所以这个套件能在几毫秒内跑完所有时序分支。
add_executable(conversation_awareness_test conversation_awareness_test.cpp ${COMMON_SOURCES})
target_include_directories(conversation_awareness_test PRIVATE ../main)
add_test(NAME conversation_awareness_test COMMAND conversation_awareness_test)

# 骨传导自身语音证据。只吃"加速度采样 + 音频包络"，不碰 I2C 与任务调度，
# 所以包络同步/不同步、运动否决、窗未满这些分支都能在主机上判定。
add_executable(bone_voice_test bone_voice_test.cpp ${COMMON_SOURCES})
target_include_directories(bone_voice_test PRIVATE ../main)
add_test(NAME bone_voice_test COMMAND bone_voice_test)

# 端侧声音事件判断（1 s 窗 × 15 维 + 规则 + 状态机）。头文件无 IDF 依赖。
# 金标准由真机采集导出（test/sound_event_golden.h），见 experiments/sound-events/。
add_executable(sound_event_test sound_event_test.cpp)
target_include_directories(sound_event_test PRIVATE ../main)
add_test(NAME sound_event_test COMMAND sound_event_test)

# 说话 vs 非说话二分类学生（92 维窗）。金标准与 main/speech_gate_model.h 由
# experiments/sound-events/export_speech_gate.py 一起生成。
add_executable(speech_gate_test speech_gate_test.cpp)
target_include_directories(speech_gate_test PRIVATE ../main)
add_test(NAME speech_gate_test COMMAND speech_gate_test)

# 双麦遥测统计（D47）：L/R 交织数据的电平差与帧内 Pearson 相关系数。
# dual_mic.hpp 头文件无 IDF 依赖，直接在主机上钉住符号与数值。
add_executable(dual_mic_test dual_mic_test.cpp)
target_include_directories(dual_mic_test PRIVATE ../main)
add_test(NAME dual_mic_test COMMAND dual_mic_test)

# 「歌是 Mac 放的」状态（B1）：host 每 1 s 续期 `music on`，TTL 失效必须 fail-open。
# 这个旗标错了不会崩，只会让设备永久以为歌在放——所以时序要在主机上钉死。
add_executable(music_state_test music_state_test.cpp)
target_include_directories(music_state_test PRIVATE ../main)
add_test(NAME music_state_test COMMAND music_state_test)
