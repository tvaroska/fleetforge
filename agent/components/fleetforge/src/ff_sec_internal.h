/*
 * ff_sec, the component-private half — NVS init, `dn/cfg` revision hook, announced
 * `secrets_rev` getter, and sleep-readiness predicate. PRIVATE (src/).
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "ff_cfg.h"
#include "ff_store.h"
#include "mqtt_client.h"

#ifdef __cplusplus
extern "C" {
#endif

#define FF_SEC_NAMESPACE "ff_sec"
#define FF_SEC_KEY_REV "_rev"

void ff_sec_init(const ff_cfg_t *cfg, const ff_cred_t *cred);
void ff_sec_on_cfg(uint32_t target_rev, esp_mqtt_client_handle_t client,
                   const char *topic_announce);
uint32_t ff_sec_rev(void);
bool ff_sec_synced(void);

#ifdef __cplusplus
}
#endif
