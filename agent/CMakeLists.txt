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

# FF_ROLLBACK_TEST — the live test of the confirm/rollback pair
# (agent/components/fleetforge/src/ff_mqtt.c, CRITICAL.md "Device-side confirm timer /
# rollback path"; the define itself is set in components/fleetforge/CMakeLists.txt, on the
# component that compiles ff_mqtt.c). OFF unless asked for, and a
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

# FF_FAULT_TEST — the failure modes FF_ROLLBACK_TEST does not cover (R2-test-1,
# docs/runbooks/agent-qemu.md -> "Driving a failure"). Empty unless asked for, and a build
# with it set is DELIBERATELY BROKEN:
#
#   bootloop  aborts in app_main on every boot, before any of our code runs. Proves the
#             BOOTLOADER puts the previous slot back (PENDING_VERIFY + reset -> ABORTED).
#   hang      sits in app_main forever, before the mqtt session. The regression image for
#             R2-fw-4: the confirm timer armed at the top of app_main must roll it back.
#
# Same suffix rule as FF_ROLLBACK_TEST (-bltest, -hangtest), so the dashboard and the
# artifact list say what the image is. Such a build is a deployable ARTIFACT only and is
# never published: serially flashed, an image boots with no rollback armed, and a
# -bltest image then aborts forever. `firmware/publish.py` refuses the suffixes.
#
# Exclusive with FF_ROLLBACK_TEST: one fault per image, or a result cannot be attributed.
# `STREQUAL ""` rather than a bare if(): CMake truthiness turns odd values into false
# constants, and an unknown value must fail the build, not silently build a normal agent.
#
#     docker build --build-arg FF_FAULT_TEST=bootloop ...   (see agent/Dockerfile)
if(DEFINED FF_FAULT_TEST AND NOT FF_FAULT_TEST STREQUAL "")
    if(FF_ROLLBACK_TEST)
        message(FATAL_ERROR
            "FF_FAULT_TEST and FF_ROLLBACK_TEST are exclusive: one fault per image")
    endif()
    if(FF_FAULT_TEST STREQUAL "bootloop")
        string(APPEND PROJECT_VER "-bltest")
    elseif(FF_FAULT_TEST STREQUAL "hang")
        string(APPEND PROJECT_VER "-hangtest")
    else()
        message(FATAL_ERROR
            "FF_FAULT_TEST must be 'bootloop' or 'hang', got '${FF_FAULT_TEST}'")
    endif()
    message(WARNING
        "FF_FAULT_TEST=${FF_FAULT_TEST}: building a DELIBERATELY BROKEN agent (${PROJECT_VER}). "
        "Upload it as a deployable artifact only. Never publish it to the flasher catalog: "
        "a serially flashed fault image has no rollback armed.")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(fleetforge-agent)
