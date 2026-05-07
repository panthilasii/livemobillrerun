# Build the Zygisk module .so for the livemobillrerun virtual camera.
#
# Build:
#   ANDROID_NDK=$HOME/Library/Android/sdk/ndk/<ver> \
#     bash ../../build_native.sh
#
# Output: ./build/<abi>/libvcam_zygisk.so
#
# Targets API 30 (the minimum that ships the Zygisk loader on AOSP-style
# Magisk; older Magisk-Riru forks would need a different entry point).

cmake_minimum_required(VERSION 3.18.1)
project(vcam_zygisk LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Strip & no-stdlib for a tiny .so — Zygisk modules are loaded into every
# zygote child, so size matters.
add_compile_options(
    -Wall
    -Wextra
    -Wno-unused-parameter
    -fvisibility=hidden
    -fvisibility-inlines-hidden
    -ffunction-sections
    -fdata-sections
)
add_link_options(
    -Wl,--gc-sections
    -Wl,--exclude-libs,ALL
)

add_library(vcam_zygisk SHARED
    main.cpp
    yuv_reader.cpp
    camera_hook.cpp
)

target_include_directories(vcam_zygisk PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
)

target_link_libraries(vcam_zygisk
    log
    android
    dl
)

# Optional inline hook engine — Dobby. Drop it under
# third_party/dobby/ and the build will link it in.
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/third_party/dobby/CMakeLists.txt")
    message(STATUS "Dobby found — enabling inline hooks.")
    set(DOBBY_GENERATE_SHARED OFF CACHE BOOL "" FORCE)
    add_subdirectory(third_party/dobby)
    target_compile_definitions(vcam_zygisk PRIVATE VCAM_HAVE_DOBBY=1)
    target_link_libraries(vcam_zygisk dobby)
else()
    message(STATUS "Dobby NOT found — building without inline hooks "
                   "(symbol-probe only). See third_party/README.md.")
endif()
