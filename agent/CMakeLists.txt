# Fleetforge agent — top-level ESP-IDF project.
#
# Built ONLY inside agent/Dockerfile, against a digest-pinned espressif/idf image
# (docs/runbooks/agent-build.md). Never build this on `prod` — design/production.md ->
# Capacity: the production VM has no room for a 5.5 GB toolchain.
#
# PROJECT_VER comes from version.txt, NOT from `git describe`: `.git` is excluded from
# the Docker build context (agent/.dockerignore), so git describe would silently resolve
# to "1" and every board would announce the same agent_version. The value ends up in
# esp_app_get_description()->version and, from R0-fw-1, in the up/announce payload.

cmake_minimum_required(VERSION 3.16)

file(STRINGS "${CMAKE_CURRENT_LIST_DIR}/version.txt" PROJECT_VER LIMIT_COUNT 1)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(fleetforge-agent)
