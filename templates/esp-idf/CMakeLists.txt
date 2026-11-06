cmake_minimum_required(VERSION 3.16)

# Firmware version reported to the Fleetforge dashboard (fw_version).
# Bump this for every build you upload; each version names one immutable image.
set(PROJECT_VER "0.1.0")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project({{APP_NAME}})
