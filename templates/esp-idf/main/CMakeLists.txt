# main.c is your firmware; fleetforge_start.c is the library's boot sequence (keep it
# unchanged). Both reach the fleetforge component through its public include/ only.
idf_component_register(SRCS "main.c" "fleetforge_start.c"
                       INCLUDE_DIRS "."
                       REQUIRES fleetforge
                                esp_driver_gpio
                                esp_event
                                esp_http_client
                                esp_netif
                                esp_system
                                esp_timer
                                json
                                nvs_flash)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
