/*
 * The library's version, for builds that have no CMake to read agent/version.txt.
 *
 * The single source of the component's version is agent/version.txt (DECISIONS
 * 2026-10-08, R3-fw-2): the agent build reads it through PROJECT_VER. The Arduino library
 * (R3-fw-3) is built by PlatformIO or the Arduino IDE, which have no such step, so the same
 * number is retyped HERE and pinned equal to agent/version.txt and to library.json's
 * "version" by tests/test_arduino_library.py. Bump all three together.
 *
 * In an Arduino build this is what up/announce reports as `agent_version` (DECISIONS
 * R3-spec-3's version note: in a library build `agent_version` is the library's version).
 * The IDF agent build never includes this header.
 */

#pragma once

#define FF_LIB_VERSION "0.4.7"
