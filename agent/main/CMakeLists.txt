# The agent is ONE component, on purpose: at R1 this whole directory becomes an
# `ff_agent` component a customer's own project pulls in, and a flat file list with no
# internal component boundaries is what makes that a move rather than a rewrite.
#
# The two `ff_net_*` adapters are BOTH compiled on every target. `ff_net_openeth.c`
# collapses to a stub that returns ESP_ERR_NOT_SUPPORTED when CONFIG_ETH_USE_OPENETH is
# off (see the bottom of that file), which is how a chip with no Ethernet still links —
# and still explains itself if someone flashes it a `link=ethernet` config.
idf_component_register(SRCS "agent_main.c"
                            "ff_cfg.c"
                            "ff_enroll.c"
                            "ff_identity.c"
                            "ff_mqtt.c"
                            "ff_net.c"
                            "ff_net_openeth.c"
                            "ff_net_wifi.c"
                            "ff_ota.c"
                            "ff_progress.c"
                            "ff_store.c"
                            "ff_time.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_eth
                                esp_hw_support
                                esp_netif
                                esp_wifi
                                esp-tls
                                esp_http_client
                                esp_https_ota
                                esp_partition
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
