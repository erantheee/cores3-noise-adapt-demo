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
