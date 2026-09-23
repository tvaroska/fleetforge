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

# FF_ROLLBACK_TEST — the live test of the confirm/rollback pair (agent/main/ff_mqtt.c,
# CRITICAL.md "Device-side confirm timer / rollback path"). OFF unless asked for, and a
# build with it ON is DELIBERATELY BROKEN: it never confirms, so it is guaranteed to roll
# back. One switch rather than two so there is no half-configured state.
#
# It renames itself. The suffix lands in esp_app_get_description()->version and therefore
# in every up/announce, which is what makes "did it roll back?" answerable from the
# dashboard instead of from a serial console — and makes a test image impossible to
# mistake for a shippable one in the artifact list.
#
#     docker build --build-arg FF_ROLLBACK_TEST=1 ...   (see agent/Dockerfile)
if(FF_ROLLBACK_TEST)
    string(APPEND PROJECT_VER "-rbtest")
    message(WARNING
        "FF_ROLLBACK_TEST is ON: building a DELIBERATELY BROKEN agent (${PROJECT_VER}). "
        "It will never confirm an OTA image. Never publish this to the flasher catalog.")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(fleetforge-agent)
