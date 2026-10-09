# The agent's own app: app_main and nothing else. It is the first consumer of the
# `fleetforge` component (../components/fleetforge, R3-fw-2), and it reaches the
# protocol ONLY through that component's public include/ — exactly what a maker's
# firmware gets. If this file ever needs a path into the component's src/, the boundary
# is wrong, not this file.
#
# The REQUIRES list after `fleetforge` is what agent_main.c includes directly.
idf_component_register(SRCS "agent_main.c"
                       INCLUDE_DIRS "."
                       REQUIRES fleetforge
                                app_update
                                esp_app_format
                                esp_event
                                esp_hw_support
                                esp_netif
                                esp_partition
                                nvs_flash)

# -Werror on THIS component only. IDF's own components are built with the upstream's
# warning posture and adding -Werror globally would fail the build on third-party code
# nobody here can fix; scoping it to our files means an unused variable or a format
# mismatch in the agent stops the build instead of shipping to a fleet. Firmware has no
# `try:` — a truncated snprintf is a corrupted MQTT topic on a thousand boards.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)

# See the FF_FAULT_TEST block in ../CMakeLists.txt (which has already refused any other
# value). The hook in agent_main.c is preprocessor-excluded from every normal build.
if(FF_FAULT_TEST STREQUAL "bootloop")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_FAULT_TEST_BOOTLOOP=1)
elseif(FF_FAULT_TEST STREQUAL "hang")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_FAULT_TEST_HANG=1)
endif()
