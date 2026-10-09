/*
 * ff_net_openeth — the OpenCores Ethernet adapter. The link that only exists in QEMU.
 *
 * `qemu-system-xtensa -M esp32 -nic user,model=open_eth` gives the emulated chip an
 * OpenCores 10 Mbps NIC behind slirp's user-mode NAT: the guest gets 10.0.2.15 by DHCP and
 * the machine running QEMU answers on 10.0.2.2. That is the whole reason this task's
 * acceptance criteria can run on a box with no ESP32 and no serial port — the emulated
 * board reaches `just up`'s API and broker over a real TCP stack.
 *
 * QEMU's open_eth has no MAC and no PHY of its own; IDF drives it with the ordinary
 * esp_eth driver plus a DP83848 PHY model, which is what the upstream
 * examples/ethernet/basic target does for this chip. `autonego_timeout_ms = 100` is not a
 * tuning knob: the emulated PHY reports link-up instantly and never completes a real
 * negotiation, so the stock 4000 ms timeout would spend four seconds failing before the
 * driver gave up and used the fixed 10 Mbps full-duplex mode anyway.
 *
 * The whole file is behind CONFIG_ETH_USE_OPENETH (set only in sdkconfig.defaults.esp32),
 * so an esp32c3/c6/s3 build compiles the stub at the bottom and a board configured with
 * `link=ethernet` says exactly why it cannot come up rather than hanging.
 */

#include "esp_log.h"
#include "ff_net_adapter.h"

static const char *TAG = "ff-eth";

#ifdef CONFIG_ETH_USE_OPENETH

#include "esp_eth.h"
#include "esp_eth_mac_openeth.h"
#include "esp_event.h"
#include "esp_netif.h"

static esp_netif_t *s_netif;
static esp_eth_handle_t s_eth;

static void on_eth_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)data;
    switch (id) {
    case ETHERNET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "phy link up; waiting for DHCP");
        break;
    case ETHERNET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "phy link down");
        break;
    default:
        break;
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    (void)id;
    const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
    ff_net_report_got_ip(event != NULL ? event->esp_netif : s_netif);
}

esp_err_t ff_net_openeth_start(const ff_cfg_t *cfg)
{
    (void)cfg; /* Ethernet needs no credentials — that is half of why QEMU can run T2. */

    eth_mac_config_t mac_config = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_config = ETH_PHY_DEFAULT_CONFIG();
    phy_config.autonego_timeout_ms = 100; /* see the file header: the emulated PHY never
                                           * negotiates, so waiting the default 4 s only
                                           * delays a link that is already up. */

    esp_eth_mac_t *mac = esp_eth_mac_new_openeth(&mac_config);
    esp_eth_phy_t *phy = esp_eth_phy_new_dp83848(&phy_config);
    if (mac == NULL || phy == NULL) {
        ESP_LOGE(TAG, "cannot instantiate the openeth mac/phy");
        return ESP_FAIL;
    }

    esp_eth_config_t eth_config = ETH_DEFAULT_CONFIG(mac, phy);
    esp_err_t err = esp_eth_driver_install(&eth_config, &s_eth);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_eth_driver_install: %s", esp_err_to_name(err));
        return err;
    }

    esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
    s_netif = esp_netif_new(&netif_config);
    if (s_netif == NULL) {
        return ESP_FAIL;
    }

    ESP_ERROR_CHECK(esp_netif_attach(s_netif, esp_eth_new_netif_glue(s_eth)));
    ESP_ERROR_CHECK(
        esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, &on_eth_event, NULL));
    ESP_ERROR_CHECK(
        esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, &on_got_ip, NULL));

    err = esp_eth_start(s_eth);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_eth_start: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "openeth started (qemu -nic user,model=open_eth)");
    return ESP_OK;
}

#else /* !CONFIG_ETH_USE_OPENETH */

esp_err_t ff_net_openeth_start(const ff_cfg_t *cfg)
{
    (void)cfg;
    /* A configuration error, not a firmware bug, and the message has to say which: the
     * board holds `link=ethernet` in a flash-time-immutable config partition, and this
     * build has no Ethernet adapter compiled in. */
    ESP_LOGE(TAG, "this build has no ethernet adapter (CONFIG_ETH_USE_OPENETH is off for "
                  "%s) but ff_cfg says link=ethernet",
             CONFIG_IDF_TARGET);
    return ESP_ERR_NOT_SUPPORTED;
}

#endif /* CONFIG_ETH_USE_OPENETH */
