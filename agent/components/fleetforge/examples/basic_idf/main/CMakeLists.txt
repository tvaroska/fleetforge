# main.c is your firmware; fleetforge_start.c is the library's boot sequence (copy it
# unchanged). Both reach the fleetforge component through its public include/ only.
idf_component_register(SRCS "main.c" "fleetforge_start.c"
                       INCLUDE_DIRS "."
                       REQUIRES fleetforge
                                esp_driver_gpio
                                esp_event
                                esp_netif
                                esp_system
                                nvs_flash)

# Zero warnings is the bar (the scripted quickstart fails on any); no -Werror in a file you
# copy, so a newer compiler's new warning cannot stop your build.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra)
