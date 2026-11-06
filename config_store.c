

#include "config_store.h"

#include <string.h>
#include <stddef.h>   /* offsetof */

#include "pico/stdlib.h"
#include "pico/flash.h"      /* flash_safe_execute */
#include "hardware/flash.h"  /* flash_range_erase/program, FLASH_*_SIZE */


#define CONFIG_FLASH_OFFSET  (PICO_FLASH_SIZE_BYTES - FLASH_SECTOR_SIZE)
#define CONFIG_MAGIC         0x56535343u   /* 'V''S''S''C' */
#define CONFIG_VERSION       1

typedef struct {
    uint32_t magic;
    uint8_t  version;
    uint8_t  server_ip[4];
    uint8_t  checksum;   /* XOR of every byte before this field */
} config_blob_t;

static uint8_t calc_checksum(const config_blob_t *c) {
    const uint8_t *p = (const uint8_t *)c;
    uint8_t x = 0;
    for (size_t i = 0; i < offsetof(config_blob_t, checksum); i++) {
        x ^= p[i];
    }
    return x;
}

bool config_store_load_server_ip(uint8_t ip_out[4]) {
    /* Flash is memory-mapped for reads at XIP_BASE + offset. */
    const config_blob_t *stored =
        (const config_blob_t *)(XIP_BASE + CONFIG_FLASH_OFFSET);

    if (stored->magic != CONFIG_MAGIC)      return false;
    if (stored->version != CONFIG_VERSION)  return false;

    config_blob_t tmp = *stored;            /* local copy for checksum */
    if (calc_checksum(&tmp) != stored->checksum) return false;

    memcpy(ip_out, stored->server_ip, 4);
    return true;
}


static void do_flash_write(void *param) {
    const config_blob_t *blob = (const config_blob_t *)param;


    uint8_t page[FLASH_PAGE_SIZE];
    memset(page, 0xFF, sizeof(page));
    memcpy(page, blob, sizeof(config_blob_t));

    flash_range_erase(CONFIG_FLASH_OFFSET, FLASH_SECTOR_SIZE);
    flash_range_program(CONFIG_FLASH_OFFSET, page, FLASH_PAGE_SIZE);
}

bool config_store_save_server_ip(const uint8_t ip[4]) {
    config_blob_t blob;
    memset(&blob, 0, sizeof(blob));
    blob.magic   = CONFIG_MAGIC;
    blob.version = CONFIG_VERSION;
    memcpy(blob.server_ip, ip, 4);
    blob.checksum = calc_checksum(&blob);

    /* 500 ms is plenty for one sector erase + page program. */
    int rc = flash_safe_execute(do_flash_write, &blob, 500);
    return rc == PICO_OK;
}
