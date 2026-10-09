# The `fleetforge` component — the device half of the protocol: enroll, the MQTT session
# (announce, heartbeat, and the four-verb update: stage, apply, confirm, rollback), the
# ff_cfg reader, the NVS credential, and the network seam. The agent in ../../main is its
# first consumer; a maker's own ESP-IDF project is the next (R3-fw-2, DECISIONS
# 2026-10-08). IDF discovers it from <project>/components/ on its own, and a consumer
# writes `REQUIRES fleetforge`. The name is part of the public surface: never rename it.
#
# include/ is the PUBLIC surface and it is ADDITIVE-ONLY from R3-fw-2, exactly like the
# wire protocol: a firmware built on it can be shipped to boards nobody can reach again,
# so nothing in it is ever removed or changed incompatibly. It holds exactly the headers
# agent_main.c includes, and every function declared there is one agent_main.c calls
# (tests/test_ota_component.py pins both).
#
# src/ is PRIVATE (PRIV_INCLUDE_DIRS): every .c, plus ff_ota.h, ff_txn.h,
# ff_net_adapter.h and the five *_internal.h halves. A consumer's main cannot include
# them, which is what keeps `ff_mqtt_publish_status`, the rollback_capable writers and the
# announce builder out of reach: a firmware that could call them could publish
# `confirmed` for an image that never confirmed, or claim a rollback it never measured.
#
# The two `ff_net_*` adapters are BOTH compiled on every target. `ff_net_openeth.c`
# collapses to a stub that returns ESP_ERR_NOT_SUPPORTED when CONFIG_ETH_USE_OPENETH is
# off (see the bottom of that file), which is how a chip with no Ethernet still links —
# and still explains itself if someone flashes it a `link=ethernet` config.
#
# The IDF deps are PRIV_REQUIRES: the public headers need only esp_err.h and
# freertos/FreeRTOS.h, which every component already has, so nothing here leaks into a
# consumer's include path.
idf_component_register(SRCS "src/ff_cfg.c"
                            "src/ff_enroll.c"
                            "src/ff_identity.c"
                            "src/ff_marker.c"
                            "src/ff_mqtt.c"
                            "src/ff_net.c"
                            "src/ff_net_openeth.c"
                            "src/ff_net_wifi.c"
                            "src/ff_ota.c"
                            "src/ff_progress.c"
                            "src/ff_store.c"
                            "src/ff_time.c"
                            "src/ff_txn.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "src"
                       PRIV_REQUIRES esp_eth
                                     esp_hw_support
                                     esp_netif
                                     esp_wifi
                                     esp-tls
                                     esp_http_client
                                     esp_https_ota
                                     esp_partition
                                     spi_flash
                                     app_update
                                     mqtt
                                     json
                                     nvs_flash
                                     mbedtls
                                     esptool_py)

# -Werror on THIS component only. IDF's own components are built with the upstream's
# warning posture and adding -Werror globally would fail the build on third-party code
# nobody here can fix; scoping it to our files means an unused variable or a format
# mismatch in the agent stops the build instead of shipping to a fleet. Firmware has no
# `try:` — a truncated snprintf is a corrupted MQTT topic on a thousand boards.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)

# See the FF_ROLLBACK_TEST block in ../../CMakeLists.txt. Compiled in only when asked for,
# so the guarded code in src/ff_mqtt.c is absent from every normal build rather than
# merely unreached. It lives HERE, with ff_mqtt.c, and not in main: a define on main's
# target never reaches this component's objects, and an FF_ROLLBACK_TEST=1 build would
# then silently confirm — a rollback test that proves nothing.
if(FF_ROLLBACK_TEST)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_ROLLBACK_TEST=1)
endif()
