# ============================================================
# CMakeLists.txt — GPS Temperature Monitor (project-vssc)
# ============================================================
#
# BUILD INSTRUCTIONS:
#   This project is designed for the Pico VS Code Extension.
#   Open this folder in VS Code, then:
#     1. Select "Pico" as the CMake kit
#     2. Click "Build" in the status bar (or Ctrl+Shift+B)
#
#   OR from the VS Code integrated terminal:
#     mkdir build && cd build
#     cmake -G Ninja ..
#     cmake --build .
#
#   Output: build/project-vssc.uf2 — drag onto the Pico!
# ============================================================

cmake_minimum_required(VERSION 3.13)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.2.0)
set(toolchainVersion 14_2_Rel1)
set(picotoolVersion 2.2.0-a4)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================

# ─── Target: Pico 2 (RP2350, ARM Cortex-M33 — most efficient for float math) ───
set(PICO_BOARD pico2 CACHE STRING "Board type")

# Pull in Raspberry Pi Pico SDK (must be before project)
include(pico_sdk_import.cmake)

project(project-vssc C CXX ASM)

# Initialise the Raspberry Pi Pico SDK
pico_sdk_init()

# ============================================================
# WIZnet ioLibrary (W5500 Ethernet Driver)
# Bundled locally in lib/ — no internet or GitHub needed.
# To update: replace the files in lib/Ethernet/ manually.
# ============================================================
set(WIZNET_DIR "${CMAKE_CURRENT_LIST_DIR}/lib/Ethernet")

if(NOT EXISTS "${WIZNET_DIR}/wizchip_conf.h")
    message(FATAL_ERROR
        "WIZnet library not found at: ${WIZNET_DIR}\n"
        "Make sure the lib/Ethernet/ folder exists with w5500.c, socket.c, wizchip_conf.c")
endif()

message(STATUS "[WIZnet] Using bundled library: ${WIZNET_DIR}")

# ─── Build WIZnet driver as a static library ────────────────────────────────
add_library(wiznet_iolibrary STATIC
    ${WIZNET_DIR}/W5500/w5500.c
    ${WIZNET_DIR}/wizchip_conf.c
    ${WIZNET_DIR}/socket.c
)

target_include_directories(wiznet_iolibrary PUBLIC
    ${WIZNET_DIR}
    ${WIZNET_DIR}/W5500
)

target_compile_definitions(wiznet_iolibrary PUBLIC
    _WIZCHIP_=W5500
)

target_compile_options(wiznet_iolibrary PRIVATE -w)

# ============================================================
# OUR APPLICATION
# ============================================================
add_executable(project-vssc
    gps_temp_monitor.c   # Main program: Core 0 loop + Core 1 entry point
    gps_parser.c         # Parses NMEA sentences from the NEO-M8N GPS module
    w5500_spi.c          # Teaches the Pico to talk to the W5500 via SPI
    tcp_client.c         # Non-blocking TCP client for Ethernet output
    config_store.c       # Persists the server IP in flash (set once via GUI)
)

pico_set_program_name(project-vssc "project-vssc")
pico_set_program_version(project-vssc "1.0")

target_include_directories(project-vssc PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}
)

# ─── Link Libraries ─────────────────────────────────────────────────────────
target_link_libraries(project-vssc
    pico_stdlib
    hardware_adc
    hardware_uart
    hardware_spi
    hardware_flash      # flash erase/program for persistent config
    pico_multicore
    pico_flash          # flash_safe_execute (dual-core-safe flash writes)
    pico_util
    wiznet_iolibrary
)

# ─── Enable USB Serial Output, disable UART stdio ───────────────────────────
# USB stdio ON  → printf() goes to USB serial (debug via PuTTY/Thonny)
# UART stdio OFF → UART0 is free, UART1 is used exclusively for GPS
pico_enable_stdio_uart(project-vssc 0)
pico_enable_stdio_usb(project-vssc 1)

# ─── Generate .uf2, .hex, .bin output files ─────────────────────────────────
pico_add_extra_outputs(project-vssc)
