/*
 * ff_net — the seam's dispatcher. Owns the "do we have an IP yet" event group, picks the
 * adapter from the config, and logs the address the board actually got.
 *
 * The address matters in the log: on a real board it is the first evidence that DHCP and
 * the access point work, and in QEMU it is how you tell slirp's NAT came up (10.0.2.15,
 * with the host reachable at 10.0.2.2). "eth link up, ip …" is the line an operator greps
 * for before believing anything the agent says afterwards.
 */

#include "ff_net.h"

#include "esp_log.h"
#include "ff_net_adapter.h"
#include "freertos/event_groups.h"

static const char *TAG = "ff-net";

#define FF_NET_GOT_IP_BIT BIT0

static EventGroupHandle_t s_events;
static ff_link_t s_link = FF_LINK_WIFI;

void ff_net_report_got_ip(esp_netif_t *netif)
{
    esp_netif_ip_info_t ip = {0};
    const char *what = ff_cfg_link_name(s_link);
    if (netif != NULL && esp_netif_get_ip_info(netif, &ip) == ESP_OK) {
        ESP_LOGI(TAG, "%s link up, ip " IPSTR " gw " IPSTR " mask " IPSTR,
                 s_link == FF_LINK_ETHERNET ? "eth" : "wifi", IP2STR(&ip.ip), IP2STR(&ip.gw),
                 IP2STR(&ip.netmask));
    } else {
        ESP_LOGI(TAG, "%s link up (address unavailable)", what);
    }
    if (s_events != NULL) {
        xEventGroupSetBits(s_events, FF_NET_GOT_IP_BIT);
    }
}

esp_err_t ff_net_bring_up(const ff_cfg_t *cfg, TickType_t timeout)
{
    if (s_events == NULL) {
        s_events = xEventGroupCreate();
        if (s_events == NULL) {
            return ESP_ERR_NO_MEM;
        }
    }
    xEventGroupClearBits(s_events, FF_NET_GOT_IP_BIT);
    s_link = cfg->link;

    esp_err_t err;
    switch (cfg->link) {
    case FF_LINK_ETHERNET:
        err = ff_net_openeth_start(cfg);
        break;
    case FF_LINK_WIFI:
    default:
        err = ff_net_wifi_start(cfg);
        break;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot start the %s link: %s", ff_cfg_link_name(cfg->link),
                 esp_err_to_name(err));
        return err;
    }

    EventBits_t bits = xEventGroupWaitBits(s_events, FF_NET_GOT_IP_BIT, pdFALSE, pdTRUE, timeout);
    if ((bits & FF_NET_GOT_IP_BIT) == 0) {
        /* Deliberately not fatal and deliberately not a reboot: the caller retries. A
         * board that reboots on a slow DHCP server loses the log line that says so. */
        ESP_LOGE(TAG, "no IP address after %u ms on the %s link", (unsigned)pdTICKS_TO_MS(timeout),
                 ff_cfg_link_name(cfg->link));
        return ESP_ERR_TIMEOUT;
    }
    return ESP_OK;
}

const char *ff_net_link_type(void)
{
    return ff_cfg_link_name(s_link);
}

bool ff_net_rssi(int *out_dbm)
{
    if (s_link != FF_LINK_WIFI) {
        return false; /* Ethernet has no radio; up/hb reports null. */
    }
    return ff_net_wifi_rssi(out_dbm);
}
