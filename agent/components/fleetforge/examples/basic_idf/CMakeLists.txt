# Fleetforge basic — the ESP-IDF flavour of the worked example (README.md).
cmake_minimum_required(VERSION 3.16)

# This firmware's version: what the fleet dashboard shows (fw_version). Change it for
# every build you upload; a version names exactly one image.
set(PROJECT_VER "1.0.0")

# The fleetforge component. In this repository it is two directories up; in your own
# project, put a copy in <project>/components/fleetforge and delete this line.
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../..")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(fleetforge_basic)
