/*
 * The library's version, in EVERY build of the component.
 *
 * ff_marker.c writes it into the library marker's `lib_version` (spec/device-protocol.md ->
 * Library marker; R3-fw-6) in the IDF agent, in a maker's own ESP-IDF project and in an
 * Arduino build alike. It is not PROJECT_VER, which in a maker's ESP-IDF project is the
 * maker's own version, and it cannot be read from agent/version.txt by CMake, because a
 * copied component has no such file.
 *
 * The single source of the component's version is agent/version.txt (DECISIONS
 * 2026-10-08, R3-fw-2): the agent build reads it through PROJECT_VER. The same number is
 * retyped HERE and pinned equal to agent/version.txt and to library.json's "version" by
 * tests/test_arduino_library.py. Bump all three together.
 *
 * In an Arduino build this is also what up/announce reports as `agent_version` (DECISIONS
 * R3-spec-3's version note: in a library build `agent_version` is the library's version).
 */

#pragma once

#define FF_LIB_VERSION "0.5.0"
