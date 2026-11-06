
#ifndef CONFIG_STORE_H
#define CONFIG_STORE_H

#include <stdint.h>
#include <stdbool.h>

/*
 * Load the persisted server IP.
 * Returns true and fills ip_out[4] if a valid saved config exists in flash;
 * returns false otherwise (caller should fall back to the compile-time default).
 */
bool config_store_load_server_ip(uint8_t ip_out[4]);

/*
 * Persist the server IP to flash.
 * MUST be called from the core that did NOT call flash_safe_execute_core_init()
 * (here: Core 0), while the other core (Core 1) has called it.
 * Returns true on success.
 */
bool config_store_save_server_ip(const uint8_t ip[4]);

#endif /* CONFIG_STORE_H */
