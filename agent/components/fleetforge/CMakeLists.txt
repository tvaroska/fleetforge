# The `fleetforge` component — the device half of the protocol: enroll, the MQTT session
# (announce, heartbeat, and the four-verb update: stage, apply, confirm, rollback), the
# ff_cfg reader, the NVS credential, and the network Port. The agent in ../../main is its
# first consumer; a maker's own ESP-IDF project is the next (R3-fw-2, DECISIONS
# 2026-10-08). IDF discovers it from <project>/components/ on its own, and a consumer
# writes `REQUIRES fleetforge`. The name is part of the public surface: never rename it.
#
# include/ is the PUBLIC surface and it is ADDITIVE-ONLY from R3-fw-2, exactly like the
# wire protocol: a firmware built on it can be shipped to boards nobody can reach again,
# so nothing in it is ever removed or changed incompatibly. It holds exactly the headers
# agent_main.c includes (ff_safe_mode.h among them since R5-fw-6), and every function
# declared there is one agent_main.c calls (tests/test_ota_component.py pins both), plus
# the maker-API headers (ff_safe_window.h, ff_selftest.h, ff_sleep.h, ff_telemetry.h) that
# a firmware's own code calls and the stock agent never does: both worked examples call
# every function those declare (DECISIONS 2026-10-15, R4-fw-2).
#
# src/ is PRIVATE (PRIV_INCLUDE_DIRS): every .c, plus ff_ota.h, ff_txn.h,
# ff_net_adapter.h and the ten *_internal.h halves. A consumer's main cannot include
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
set(_ff_embed_txtfiles "")
set(_ff_use_embedded_pubkeys FALSE)
if(FF_OTA_SIGNING_PUBKEY_PRIMARY_FILE OR FF_OTA_SIGNING_PUBKEY_SECONDARY_FILE)
    if(NOT FF_OTA_SIGNING_PUBKEY_PRIMARY_FILE OR NOT FF_OTA_SIGNING_PUBKEY_SECONDARY_FILE)
        message(FATAL_ERROR "Provide both FF_OTA_SIGNING_PUBKEY_PRIMARY_FILE and FF_OTA_SIGNING_PUBKEY_SECONDARY_FILE")
    endif()
    set(_ff_primary_pem_dst "${CMAKE_CURRENT_BINARY_DIR}/ff_ota_pubkey_primary.pem")
    set(_ff_secondary_pem_dst "${CMAKE_CURRENT_BINARY_DIR}/ff_ota_pubkey_secondary.pem")
    configure_file("${FF_OTA_SIGNING_PUBKEY_PRIMARY_FILE}" "${_ff_primary_pem_dst}" COPYONLY)
    configure_file("${FF_OTA_SIGNING_PUBKEY_SECONDARY_FILE}" "${_ff_secondary_pem_dst}" COPYONLY)
    list(APPEND _ff_embed_txtfiles "${_ff_primary_pem_dst}" "${_ff_secondary_pem_dst}")
    set(_ff_use_embedded_pubkeys TRUE)
endif()

idf_component_register(SRCS "src/ff_cfg.c"
                            "src/ff_delta.c"
                            "src/ff_enroll.c"
                            "src/ff_identity.c"
                            "src/ff_log.c"
                            "src/ff_marker.c"
                            "src/ff_mqtt.c"
                            "src/ff_mqtt_boot.c"
                            "src/ff_mqtt_pub.c"
                            "src/ff_mqtt_rx.c"
                            "src/ff_net.c"
                            "src/ff_net_openeth.c"
                            "src/ff_net_wifi.c"
                            "src/ff_net_wifi_improv.c"
                            "src/ff_ota.c"
                            "src/ff_ota_cargo.c"
                            "src/ff_ota_verify.c"
                            "src/ff_progress.c"
                            "src/ff_safe_mode.c"
                            "src/ff_safe_window.c"
                            "src/ff_sec.c"
                            "src/ff_selftest.c"
                            "src/ff_sleep.c"
                            "src/ff_store.c"
                            "src/ff_telemetry.c"
                            "src/ff_time.c"
                            "src/ff_txn.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "src"
                       EMBED_TXTFILES ${_ff_embed_txtfiles}
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
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror -Wframe-larger-than=1024)

# See the FF_ROLLBACK_TEST block in ../../CMakeLists.txt. Compiled in only when asked for,
# so the guarded code in src/ff_mqtt.c is absent from every normal build rather than
# merely unreached. It lives HERE, with ff_mqtt.c, and not in main: a define on main's
# target never reaches this component's objects, and an FF_ROLLBACK_TEST=1 build would
# then silently confirm — a rollback test that proves nothing.
if(FF_ROLLBACK_TEST)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_ROLLBACK_TEST=1)
endif()

if(_ff_use_embedded_pubkeys)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_OTA_EMBEDDED_PUBKEYS=1)
endif()

if(FF_OTA_PRODUCTION_BUILD OR CONFIG_FF_OTA_PRODUCTION_BUILD)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_OTA_PRODUCTION_BUILD=1)
    set(_ff_prod_primary "")
    set(_ff_prod_secondary "")
    if(_ff_use_embedded_pubkeys)
        file(READ "${_ff_primary_pem_dst}" _ff_prod_primary)
        file(READ "${_ff_secondary_pem_dst}" _ff_prod_secondary)
    elseif(CONFIG_FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM AND CONFIG_FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM)
        set(_ff_prod_primary "${CONFIG_FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM}")
        set(_ff_prod_secondary "${CONFIG_FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM}")
        target_compile_definitions(${COMPONENT_LIB} PRIVATE FF_OTA_KCONFIG_PUBKEYS_VALID=1)
    else()
        message(FATAL_ERROR "FF_OTA_PRODUCTION_BUILD requires both primary and secondary production signing public key PEMs via FF_OTA_SIGNING_PUBKEY_{PRIMARY,SECONDARY}_FILE (EMBED_TXTFILES) or non-empty CONFIG_FF_OTA_SIGNING_PUBKEY_{PRIMARY,SECONDARY}_PEM")
    endif()
    foreach(_pem_slot _ff_prod_primary _ff_prod_secondary)
        set(_pem_val "${${_pem_slot}}")
        if(NOT _pem_val MATCHES "-----BEGIN PUBLIC KEY-----")
            message(FATAL_ERROR "FF_OTA_PRODUCTION_BUILD slot ${_pem_slot} must contain a PEM SubjectPublicKeyInfo public key")
        endif()
        if(_pem_val MATCHES "bHg1tVC28a9y8SbZY0jis7OKKWxr" OR _pem_val MATCHES "wJct9NbOiOIbnB/ToYTvq02EBrzs")
            message(FATAL_ERROR "FF_OTA_PRODUCTION_BUILD refuses committed development signing keys in ${_pem_slot}")
        endif()
    endforeach()
endif()
