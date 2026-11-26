cmake_minimum_required(VERSION 3.22)
# ===== Build variant selection =====
#   OpenPixelOSD      -> base OSD (no VTX drivers)
#   OpenPixelOSD_VTX  -> OSD + VTX (RTC6705 / PA / MSP VTX)
set(BUILD_VARIANT "" CACHE STRING "Build variant: VTX")

set(TARGET_MCU "" CACHE STRING "Target MCU (e.g., STM32G431, STM32G474)")
# set default to G431 if not specified
if(NOT TARGET_MCU)
    set(TARGET_MCU STM32G431)
endif()

message(STATUS "Selected TARGET_MCU=${TARGET_MCU} BUILD_VARIANT=${BUILD_VARIANT}")

if(UNIX AND NOT APPLE)
    # Linux
    set(OS_SUPPORTED TRUE)
elseif(APPLE)
    # macOS
    set(OS_SUPPORTED TRUE)
else()
    set(OS_SUPPORTED FALSE)
endif()

if(OS_SUPPORTED)
    execute_process(
            COMMAND git rev-parse --abbrev-ref HEAD
            COMMAND cut -c1-10
            OUTPUT_VARIABLE GIT_BRANCH
            RESULT_VARIABLE BRANCH_RESULT
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET
    )
    if(BRANCH_RESULT EQUAL 0)
        message(STATUS "Git branch: ${GIT_BRANCH}")
        add_definitions(-DGIT_BRANCH=\"${GIT_BRANCH}\")
    else()
        message(STATUS "Git branch not found")
        add_definitions(-DGIT_BRANCH=\"\")
    endif()

    execute_process(
            COMMAND git rev-parse --short=8 HEAD
            OUTPUT_VARIABLE GIT_HASH
            RESULT_VARIABLE HASH_RESULT
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET
    )
    if(HASH_RESULT EQUAL 0)
        message(STATUS "Git hash: ${GIT_HASH}")
        add_definitions(-DGIT_HASH=\"${GIT_HASH}\")
    else()
        message(STATUS "Git hash not found")
        add_definitions(-DGIT_HASH=\"-.-.-\")
    endif()

    execute_process(
            COMMAND git describe --tags --abbrev=0
            OUTPUT_VARIABLE GIT_TAG
            RESULT_VARIABLE TAG_RESULT
            OUTPUT_STRIP_TRAILING_WHITESPACE
            ERROR_QUIET
    )
    if(TAG_RESULT EQUAL 0 AND NOT GIT_TAG STREQUAL "")
        message(STATUS "Git tag: ${GIT_TAG}")
        add_definitions(-DGIT_TAG=\"${GIT_TAG}\")
    elseif(HASH_RESULT EQUAL 0 AND NOT GIT_HASH STREQUAL "")
        message(STATUS "Git tag not found; using commit hash ${GIT_HASH}")
        add_definitions(-DGIT_TAG=\"${GIT_HASH}\")
    else()
        message(STATUS "Git tag/hash not found")
        add_definitions(-DGIT_TAG=\"unknown\")
    endif()
endif()

# Setup compiler settings
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "Debug")
endif()

# Set the project name
set(CMAKE_PROJECT_NAME OpenPixelOSD)

# Include toolchain file
include("cmake/gcc-arm-none-eabi.cmake")

# Enable compile command to ease indexing with e.g. clangd
set(CMAKE_EXPORT_COMPILE_COMMANDS TRUE)

project(${CMAKE_PROJECT_NAME} LANGUAGES C ASM)
message(STATUS "Build type: " ${CMAKE_BUILD_TYPE})

add_executable(${CMAKE_PROJECT_NAME})


if(TARGET_MCU STREQUAL "STM32G431")
    message(STATUS "Building for STM32G431")
    add_compile_definitions(
            MCU_TYPE=\"STM32G431\"
            STM32G431xx
            LOW_RAM
    )
    set(LINKER_SCRIPT ${CMAKE_SOURCE_DIR}/stm32g431cbux_flash.ld)
    set(STARTUP_ASSEMBLY_FILE ./src/stm32g4xx/startup_stm32g431xx.s)
elseif(TARGET_MCU STREQUAL "STM32G474")
    message(STATUS "Building for STM32G474")
    add_compile_definitions(
            MCU_TYPE=\"STM32G474\"
            STM32G474xx
            HIGH_RAM
    )
    set(LINKER_SCRIPT ${CMAKE_SOURCE_DIR}/stm32g474ceux_flash.ld)
    set(STARTUP_ASSEMBLY_FILE ./src/stm32g4xx/startup_stm32g474xx.s)
    set(SOURCE src/fonts/font_system.c)
else()
    message(FATAL_ERROR "Unsupported MCU type: ${TARGET_MCU}")
endif()

# OSD font face: which 12x18 2bpp font_data[] table to compile in.
#   default  = original Betaflight font (byte-identical to src/fonts/font_bf_default.h)
#   plexmono | adwaita | iosevka = custom faces (see python/make_pixel_font_2bpp.py
#              + python/merge_font.py). The lowercase code points 0x61-0x7A (Betaflight
#              OSD symbols) are preserved in every face.
set(OSD_FONT "terminus" CACHE STRING "OSD font face: default|terminus")
set_property(CACHE OSD_FONT PROPERTY STRINGS default terminus)
set(_OSD_FONT_SRC src/fonts/font_data_${OSD_FONT}.c)
if(NOT EXISTS "${CMAKE_SOURCE_DIR}/${_OSD_FONT_SRC}")
    message(FATAL_ERROR "OSD_FONT='${OSD_FONT}' -> ${_OSD_FONT_SRC} not found")
endif()
message(STATUS "OSD font face: ${OSD_FONT}")

if(BUILD_VARIANT STREQUAL "VTX")
    message(STATUS "Building with VTX support")
    set(BUILD_NAME "_VTX")
    add_compile_definitions(BUILD_VARIANT_VTX)
    set(VTX_SRC
            ./src/rtc6705.c
            ./src/rf_pa.c
            ./src/vtx_msp.c)
endif()

# Diagnostic build: force a static camera passthrough (OPAMP1 follows PA7 -> PA2).
# No sync detection, no video generator, no OSD overlay. Used to isolate the analog
# video-input path from the sync-detection logic. Enable with -DVIDEO_PASSTHROUGH_TEST=ON.
option(VIDEO_PASSTHROUGH_TEST "Diagnostic: static camera passthrough only (no sync/overlay)" OFF)
if(VIDEO_PASSTHROUGH_TEST)
    message(STATUS "DIAGNOSTIC build: VIDEO_PASSTHROUGH_TEST enabled (camera -> OPAMP1 -> output, no OSD)")
    add_compile_definitions(VIDEO_PASSTHROUGH_TEST)
endif()

# Diagnostic: disable the internal video generator + its watchdog, so the OSD runs the full
# overlay machinery but locks purely on COMP3 camera sync (no camera<->generator flip-flop).
option(VIDEO_CAMERA_ONLY "Diagnostic: camera-sync only, no video generator fallback" OFF)
if(VIDEO_CAMERA_ONLY)
    message(STATUS "DIAGNOSTIC build: VIDEO_CAMERA_ONLY enabled (no generator fallback)")
    add_compile_definitions(VIDEO_CAMERA_ONLY)
endif()

# Debug tuning harness (no scope needed): holds the splash/logo on screen and live-sweeps the
# COMP3 sync threshold, printing the current value + the boot reset-cause (PIN/BOR) so the
# warm-reset OSD horizontal smear can be diagnosed by eye. See video_overlay.c (OSD_TUNING_MODE).
option(OSD_TUNING_MODE "Debug: hold splash + live sync-threshold sweep with on-screen readout" OFF)
if(OSD_TUNING_MODE)
    message(STATUS "DEBUG build: OSD_TUNING_MODE enabled (splash held + threshold sweep)")
    add_compile_definitions(OSD_TUNING_MODE)
endif()

# Boot "wave" animation: after the splash clears, a diagonal band of '+' sweeps top-left to
# bottom-right (~300 ms). See video_overlay.c (OSD_BOOT_ANIMATION / video_boot_animation_update).
option(OSD_BOOT_ANIMATION "Power-up OSD '+' wave animation" ON)
if(OSD_BOOT_ANIMATION)
    message(STATUS "OSD boot fade-in animation enabled")
    add_compile_definitions(OSD_BOOT_ANIMATION)
endif()

# Auto-calibrate the camera sync threshold at power-up / camera appearance: sweeps DAC1_CH1 and
# picks the centre of the widest window in which the line parser recognises (nearly) every line.
# Falls back to VIDEO_SYNC_THRESHOLD_MV when no window is found. See src/sync_calib.h and
# doc/camera-waveforms/. Forced OFF in OSD_TUNING_MODE (which sweeps the threshold itself).
option(OSD_AUTO_SYNC_CAL "Auto-calibrate the camera sync threshold" ON)
if(OSD_AUTO_SYNC_CAL AND NOT OSD_TUNING_MODE AND NOT VIDEO_PASSTHROUGH_TEST)
    message(STATUS "OSD sync-threshold auto-calibration enabled")
    add_compile_definitions(OSD_AUTO_SYNC_CAL)
endif()

# COMP3/COMP4 video-sync detection threshold (mV). Empty = firmware default (162 mV).
# Sweep per-camera to fix intermittent sync lock, e.g. -DVIDEO_SYNC_THRESHOLD_MV=200
set(VIDEO_SYNC_THRESHOLD_MV "" CACHE STRING "Video sync detection threshold in mV")
if(VIDEO_SYNC_THRESHOLD_MV)
    message(STATUS "Video sync threshold: ${VIDEO_SYNC_THRESHOLD_MV} mV")
    add_compile_definitions(VIDEO_SYNC_THRESHOLD_MV=${VIDEO_SYNC_THRESHOLD_MV})
endif()

# COMP3/COMP4 input hysteresis: NONE / LOW / MEDIUM / HIGH (10/40/70 mV). Empty = firmware default (LOW).
set(VIDEO_SYNC_HYSTERESIS "" CACHE STRING "COMP hysteresis: NONE/LOW/MEDIUM/HIGH")
if(VIDEO_SYNC_HYSTERESIS)
    message(STATUS "COMP sync hysteresis: ${VIDEO_SYNC_HYSTERESIS}")
    add_compile_definitions(VIDEO_SYNC_HYSTERESIS=LL_COMP_HYSTERESIS_${VIDEO_SYNC_HYSTERESIS})
endif()

# OSD pixel output levels in mV (see src/video_config.h). Empty = firmware default.
foreach(_osd_lvl VIDEO_DAC_BLACK_MV VIDEO_DAC_GRAY_MV VIDEO_DAC_WHITE_MV)
    set(${_osd_lvl} "" CACHE STRING "OSD pixel level ${_osd_lvl} in mV")
    if(${_osd_lvl})
        message(STATUS "${_osd_lvl} = ${${_osd_lvl}} mV")
        add_compile_definitions(${_osd_lvl}=${${_osd_lvl}})
    endif()
endforeach()

add_compile_definitions(
        USE_FULL_LL_DRIVER
        USE_HAL_DRIVER
)
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -T${LINKER_SCRIPT}")

add_subdirectory(cmake/stm32)

# Link directories setup
target_link_directories(${CMAKE_PROJECT_NAME} PRIVATE
)

# Add sources to executable
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
        ${STARTUP_ASSEMBLY_FILE}
        ${SOURCE}
        ${VTX_SRC}
        ./src/stm32g4xx/stm32g4xx_it.c
        ./src/stm32g4xx/system_stm32g4xx.c
        ./src/stm32g4xx/sysmem.c
        ./src/stm32g4xx/syscalls.c
        ./src/stm32g4xx/system.c
        ./src/msp/msp.c
        ./src/gpio.c
        ./src/dac.c
        ./src/adc.c
        ./src/dma.c
        ./src/opamp.c
        ./src/tim.c
        ./src/comp.c
        ./src/uart.c
        ./src/video_gen.c
        ./src/video_overlay.c
        ./src/canvas_char.c
        ./src/msp_displayport.c
        ./src/fonts/update_font.c
        ${_OSD_FONT_SRC}
        ./src/video_graphics.c
        ./src/main.c

)

# Add include paths
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE
        ./src/stm32g4xx
        ./src/msp
        ./src
)

# Add project symbols (macros)
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE

)

# Add linked libraries
target_link_libraries(${CMAKE_PROJECT_NAME}
        stm32cubemx

)


# Generate hex and bin files
set(HEX_FILE ${CMAKE_PROJECT_NAME}_${TARGET_MCU}${BUILD_NAME}.hex)
set(BIN_FILE ${CMAKE_PROJECT_NAME}_${TARGET_MCU}${BUILD_NAME}.bin)

add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
        COMMAND ${CMAKE_OBJCOPY} -Oihex $<TARGET_FILE:${PROJECT_NAME}> ${PROJECT_BINARY_DIR}/${HEX_FILE}
        COMMAND ${CMAKE_OBJCOPY} -Obinary $<TARGET_FILE:${PROJECT_NAME}> ${PROJECT_BINARY_DIR}/${BIN_FILE}
        COMMAND ${CMAKE_SIZE} $<TARGET_FILE:${PROJECT_NAME}>)

# make a zip file containing the hex, bin, map and elf
set(ZIP_NAME "${CMAKE_PROJECT_NAME}_${TARGET_MCU}_${CMAKE_BUILD_TYPE}${BUILD_NAME}")
set(MAP_FILE "${PROJECT_BINARY_DIR}/${CMAKE_PROJECT_NAME}.map")

# Create ZIP after build completes
add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E echo "Creating ${ZIP_NAME}.zip"
        COMMAND ${CMAKE_COMMAND} -E tar "cf" "${ZIP_NAME}.zip" --format=zip
        "${HEX_FILE}"
        "${BIN_FILE}"
        "${MAP_FILE}"
        "$<TARGET_FILE:${PROJECT_NAME}>"
        WORKING_DIRECTORY ${PROJECT_BINARY_DIR}
        COMMENT "Creating ZIP archive: ${ZIP_NAME}.zip"
)

# Flash the device using dfu-util
add_custom_target(flash
        COMMAND dfu-util -a 0 -s 0x08000000:leave -D ${PROJECT_BINARY_DIR}/${BIN_FILE} -R )
