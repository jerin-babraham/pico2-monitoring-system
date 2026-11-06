

#include "tcp_client.h"

#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "pico/stdlib.h"
#include "hardware/sync.h"   /* __dmb() cross-core barrier */

#include "wizchip_conf.h"
#include "socket.h"

#include "net_config.h"
#include "config_store.h"
#include "gps_parser.h"      /* gps_get_baud() for the identity banner */

/* server_ip is owned by Core 1; SERVER_IP is only the compile-time default. */
static uint8_t server_ip[4]     = SERVER_IP;
static bool    is_connected     = false;
static int     cooldown_counter = 0;

/* Core 0 stages an IP change here; Core 1 applies it in tcp_send_line(). */
static volatile uint8_t pending_ip[4];
static volatile bool    ip_change_pending = false;


static bool tcp_reconnect_fast(void) {
    if (getSn_SR(TCP_SOCKET) != SOCK_CLOSED) {
        disconnect(TCP_SOCKET);
        close(TCP_SOCKET);
        sleep_ms(50);
    }

    if (!(getPHYCFGR() & 0x01)) {   /* no Ethernet link */
        is_connected = false;
        return false;
    }

    if (socket(TCP_SOCKET, Sn_MR_TCP, LOCAL_PORT, 0) != TCP_SOCKET) {
        is_connected = false;
        return false;
    }

    setRTR(2000);   /* 200 ms per retry (units of 100 µs) */
    setRCR(2);      /* up to 2 retries */

    if (connect(TCP_SOCKET, server_ip, SERVER_PORT) != SOCK_OK) {
        close(TCP_SOCKET);
        is_connected = false;
        return false;
    }

    sleep_ms(50);
    if (getSn_SR(TCP_SOCKET) != SOCK_ESTABLISHED) {
        close(TCP_SOCKET);
        is_connected = false;
        return false;
    }

    printf("[TCP] Connected to %d.%d.%d.%d:%d\n",
           server_ip[0], server_ip[1], server_ip[2], server_ip[3], SERVER_PORT);

    /* Identity banner (no "seq" field, so the PC treats it as status). Carries
     * the auto-detected GPS baud so a TCP-only GUI can display it too. */
    char banner[128];
    int  blen = snprintf(banner, sizeof(banner),
        "{\"device\":\"%s\",\"version\":\"%s\",\"gps_baud\":%lu,\"msg\":\"ready\"}\n",
        DEVICE_ID, FW_VERSION, (unsigned long)gps_get_baud());
    if (blen > 0 && blen < (int)sizeof(banner)) {
        send(TCP_SOCKET, (uint8_t *)banner, (uint16_t)blen);
    }

    is_connected     = true;
    cooldown_counter = 0;
    return true;
}


void tcp_log_status(void) {
    uint8_t phy = getPHYCFGR();
    uint8_t sr  = getSn_SR(TCP_SOCKET);
    printf("[NET] PHY=0x%02X link=%s | sock=0x%02X | conn=%s | "
           "target=%d.%d.%d.%d:%d\n",
           phy, (phy & 0x01) ? "UP" : "DOWN", sr,
           is_connected ? "yes" : "no",
           server_ip[0], server_ip[1], server_ip[2], server_ip[3], SERVER_PORT);
}


void tcp_set_server_ip(const uint8_t ip[4]) {
    pending_ip[0] = ip[0];
    pending_ip[1] = ip[1];
    pending_ip[2] = ip[2];
    pending_ip[3] = ip[3];
    __dmb();                    /* publish the bytes before the flag */
    ip_change_pending = true;
}


bool tcp_init(void) {
    uint8_t saved_ip[4];
    if (config_store_load_server_ip(saved_ip)) {
        memcpy(server_ip, saved_ip, 4);
        printf("[TCP] Server IP from flash: %d.%d.%d.%d\n",
               server_ip[0], server_ip[1], server_ip[2], server_ip[3]);
    } else {
        printf("[TCP] Server IP from default: %d.%d.%d.%d\n",
               server_ip[0], server_ip[1], server_ip[2], server_ip[3]);
    }

    wiz_NetInfo net_info = {
        .mac  = PICO_MAC,
        .ip   = PICO_IP,
        .sn   = PICO_SUBNET,
        .gw   = PICO_GATEWAY,
        .dns  = PICO_DNS,
        .dhcp = NETINFO_STATIC,
    };
    wizchip_setnetinfo(&net_info);

    wiz_NetInfo verify;
    wizchip_getnetinfo(&verify);
    printf("[TCP] Pico IP %d.%d.%d.%d -> server %d.%d.%d.%d:%d\n",
           verify.ip[0], verify.ip[1], verify.ip[2], verify.ip[3],
           server_ip[0], server_ip[1], server_ip[2], server_ip[3], SERVER_PORT);

    return tcp_reconnect_fast();
}


int format_sensor_json(char *buf, int buflen, const sensor_record_t *rec) {
    int n;

    if (rec->gps_locked) {
   
        char gps_time[40];
        if (rec->year >= 2024) {
            snprintf(gps_time, sizeof(gps_time),
                "\"%04u-%02u-%02uT%02u:%02u:%02uZ\"",
                rec->year, rec->month, rec->day,
                rec->hour, rec->minute, rec->second);
        } else {
            strcpy(gps_time, "null");
        }

        n = snprintf(buf, buflen,
            "{\"seq\":%" PRIu32 ",\"ts_uptime_us\":%" PRIu64 ","
            "\"gps_locked\":true,\"gps_stale\":%s,"
            "\"gps_time\":%s,"
            "\"lat\":%.6f,\"lon\":%.6f,\"alt\":%.1f,"
            "\"sats\":%u,\"hdop\":%.1f,\"fix_q\":%u,\"temp_c\":%.2f}\n",
            rec->seq, rec->uptime_us,
            rec->gps_stale ? "true" : "false",
            gps_time,
            rec->latitude, rec->longitude, rec->altitude,
            (unsigned)rec->satellites, rec->hdop,
            (unsigned)rec->fix_quality, rec->temperature_c);
    } else {
        n = snprintf(buf, buflen,
            "{\"seq\":%" PRIu32 ",\"ts_uptime_us\":%" PRIu64 ","
            "\"gps_locked\":false,\"gps_stale\":false,\"gps_time\":null,"
            "\"lat\":null,\"lon\":null,\"alt\":null,"
            "\"sats\":%u,\"hdop\":null,\"fix_q\":0,\"temp_c\":%.2f}\n",
            rec->seq, rec->uptime_us,
            (unsigned)rec->satellites, rec->temperature_c);
    }

    if (n < 0 || n >= buflen) return -1;
    return n;
}


bool tcp_send_line(const char *line, int len) {
    if (ip_change_pending) {
        __dmb();
        memcpy(server_ip, (const void *)pending_ip, 4);
        ip_change_pending = false;
        is_connected      = false;
        cooldown_counter  = 0;
        printf("[TCP] Server IP changed at runtime: %d.%d.%d.%d\n",
               server_ip[0], server_ip[1], server_ip[2], server_ip[3]);
    }

    if (!(getPHYCFGR() & 0x01)) {   /* no link */
        is_connected = false;
        return false;
    }

    if (!is_connected || getSn_SR(TCP_SOCKET) != SOCK_ESTABLISHED) {
        is_connected = false;
        if (cooldown_counter > 0) {
            cooldown_counter--;
            return false;
        }
        if (!tcp_reconnect_fast()) {
            cooldown_counter = RECONNECT_COOLDOWN;
            return false;
        }
    }

    if (send(TCP_SOCKET, (uint8_t *)line, (uint16_t)len) <= 0) {
        is_connected     = false;
        cooldown_counter = RECONNECT_COOLDOWN;
        return false;
    }
    return true;
}
