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
