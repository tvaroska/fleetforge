cmake_minimum_required(VERSION 3.16)

# Firmware version reported to the Fleetforge dashboard (fw_version).
# Bump this for every build you upload; each version names one immutable image.
set(PROJECT_VER "0.1.0")

# Set -DFF_OTA_PRODUCTION_BUILD=1 on release builds after configuring custom signing keys.
if(FF_OTA_PRODUCTION_BUILD)
    add_compile_definitions(FF_OTA_PRODUCTION_BUILD=1)
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project({{APP_NAME}})
