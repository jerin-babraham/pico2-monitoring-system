
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "pico/stdio.h"
#include "pico/time.h"
#include "pico/stdio_usb.h"
#include "pico/multicore.h"
#include "pico/flash.h"        /* flash_safe_execute_core_init() */
#include "pico/util/queue.h"
#include "hardware/adc.h"

#include "gps_temp_monitor.h"
#include "gps_parser.h"
#include "w5500_spi.h"
#include "tcp_client.h"
#include "config_store.h"

static queue_t data_queue;

#define QUEUE_DEPTH      16     // backpressure tolerance
#define GPS_LOCK_TIMEOUT 10     // GPS can pause 3-5s during reacquisition
#define TEMP_SAMPLES     4      // ADC readings averaged per temperature sample


void core1_entry(void) {
    printf("[Core1] Started. Initializing TCP (non-blocking)...\n");

    /* Register Core 1 as the flash lockout victim so Core 0 can safely
     * erase/program flash (config_store_save_server_ip) while Core 1 runs. */
    if (!flash_safe_execute_core_init()) {
        printf("[Core1] WARNING: flash lockout init failed; saving IP is unsafe.\n");
    }

    if (!tcp_init()) {
        printf("[Core1] TCP not available. Serial-only mode.\n");
    }

    printf("[Core1] Entering send loop. Waiting for sensor data...\n");

    sensor_record_t record;
    char            line[JSON_LINE_MAX];

    while (true) {
        queue_remove_blocking(&data_queue, &record);

        int len = format_sensor_json(line, sizeof(line), &record);
        if (len <= 0) continue;

        fwrite(line, 1, (size_t)len, stdout);  /* USB serial */
        tcp_send_line(line, len);              /* Ethernet (skipped if down) */

        /* Periodic network diagnostic (~every 10 s) so the W5500/link/socket
         * state is visible in the live stream without a reboot. */
        if (record.seq % 10 == 0) tcp_log_status();
    }
}



static void handle_usb_command(const char *cmd) {
    unsigned a, b, c, d;

    if (strncmp(cmd, "SETIP ", 6) == 0) {
        if (sscanf(cmd + 6, "%u.%u.%u.%u", &a, &b, &c, &d) == 4 &&
            a < 256 && b < 256 && c < 256 && d < 256) {
            uint8_t ip[4] = { (uint8_t)a, (uint8_t)b, (uint8_t)c, (uint8_t)d };
            bool saved = config_store_save_server_ip(ip);  /* persist */
            tcp_set_server_ip(ip);                         /* apply on Core 1 */
            printf("{\"msg\":\"ip_set\",\"server_ip\":\"%u.%u.%u.%u\","
                   "\"saved\":%s}\n", a, b, c, d, saved ? "true" : "false");
        } else {
            printf("{\"msg\":\"error\",\"detail\":\"bad SETIP format, "
                   "expected SETIP a.b.c.d\"}\n");
        }
    } else if (strcmp(cmd, "GETIP") == 0) {
        uint8_t ip[4];
        if (config_store_load_server_ip(ip)) {
            printf("{\"msg\":\"ip\",\"server_ip\":\"%u.%u.%u.%u\","
                   "\"source\":\"flash\"}\n", ip[0], ip[1], ip[2], ip[3]);
        } else {
            printf("{\"msg\":\"ip\",\"source\":\"default\"}\n");
        }
    }
    /* Unknown commands are ignored silently. */
}


static void poll_usb_commands(void) {
    static char cmd_buf[64];
    static int  cmd_len = 0;

    int ch;
    while ((ch = getchar_timeout_us(0)) != PICO_ERROR_TIMEOUT) {
        if (ch == '\r') {
            continue;
        } else if (ch == '\n') {
            cmd_buf[cmd_len] = '\0';
            if (cmd_len > 0) {
                handle_usb_command(cmd_buf);
            }
            cmd_len = 0;
        } else if (cmd_len < (int)sizeof(cmd_buf) - 1) {
            cmd_buf[cmd_len++] = (char)ch;
        } else {
            cmd_len = 0;   /* overflow — discard the runaway line */
        }
    }
}


/* Average several ADC reads of the on-chip temperature sensor (°C). */
static float read_internal_temperature(void) {
    const float conversion_factor = 3.3f / (1 << 12);
    uint32_t sum = 0;

    adc_select_input(4);

    for (int i = 0; i < TEMP_SAMPLES; i++) {
        sum += adc_read();
    }

    float avg_raw = (float)sum / (float)TEMP_SAMPLES;
    float voltage = avg_raw * conversion_factor;
    return 27.0f - (voltage - 0.706f) / 0.001721f;
}


int main(void) {
    stdio_init_all();

#if LIB_PICO_STDIO_USB
    for (int i = 0; i < 20; i++) {
        if (stdio_usb_connected()) break;
        sleep_ms(100);
    }
#endif

    /* Device banner (no "seq" field, so the PC treats it as status not data). */
    printf("{\"device\":\"%s\",\"version\":\"%s\",\"msg\":\"ready\"}\n",
           DEVICE_ID, FW_VERSION);
    printf("[Core0] Device: %s v%s\n", DEVICE_ID, FW_VERSION);
    printf("[Core0] Plug into any USB port — Python will auto-detect this device.\n");

    adc_init();
    adc_set_temp_sensor_enabled(true);
    adc_select_input(4);
    printf("[Core0] ADC initialized. Temperature sensor on channel 4.\n");
    printf("[Core0] Temperature averaging: %d samples per reading.\n", TEMP_SAMPLES);

    gps_init();

    /* Report the auto-detected GPS baud as a status line (no "seq" field, so the
     * PC treats it as status; the GUI shows which rate the module locked at). */
    printf("{\"msg\":\"gps_ready\",\"gps_baud\":%lu}\n",
           (unsigned long)gps_get_baud());

    w5500_spi_init();

    queue_init(&data_queue, sizeof(sensor_record_t), QUEUE_DEPTH);
    printf("[Core0] Inter-core queue initialized. Depth: %d records.\n", QUEUE_DEPTH);

    printf("[Core0] Launching Core 1 (serial + TCP sender)...\n");
    multicore_launch_core1(core1_entry);

    printf("[Core0] Entering sensor loop. Sampling every 1 second.\n\n");

    uint32_t        record_number = 0;
    absolute_time_t next_sample   = get_absolute_time();

    static gps_data_t last_good_gps = {0};
    static int        gps_fix_age   = GPS_LOCK_TIMEOUT + 1;

    while (true) {
        next_sample = delayed_by_ms(next_sample, 1000);
        record_number++;

        /* Check for host commands (e.g. SETIP from the GUI) every cycle. */
        poll_usb_commands();

        uint64_t now_us      = time_us_64();
        float    temperature = read_internal_temperature();

        gps_update();

        gps_data_t gps_data        = {0};
        bool       gps_has_new_data = gps_get_data(&gps_data);
        bool       gps_is_stale    = false;

        if (gps_has_new_data && gps_data.valid) {
            last_good_gps = gps_data;   /* fresh fix — refresh cache */
            gps_fix_age   = 0;
        } else if (gps_fix_age <= GPS_LOCK_TIMEOUT) {
           
            gps_fix_age++;
        }


        bool gps_locked = (gps_fix_age <= GPS_LOCK_TIMEOUT);

        if (gps_locked && !gps_has_new_data) {
            gps_is_stale = true;
        }

        sensor_record_t rec = {0};
        rec.temperature_c   = temperature;
        rec.uptime_us       = now_us;
        rec.seq             = record_number;
        rec.gps_locked      = gps_locked;
        rec.gps_stale       = gps_is_stale;
        rec.satellites      = last_good_gps.satellites;
        rec.fix_quality     = last_good_gps.fix_quality;
        rec.hdop            = last_good_gps.hdop;

        if (gps_locked) {
            /* Use UTC time directly — PC converts to IST for display */
            rec.hour      = last_good_gps.hour;
            rec.minute    = last_good_gps.minute;
            rec.second    = last_good_gps.second;
            rec.day       = last_good_gps.day;
            rec.month     = last_good_gps.month;
            rec.year      = last_good_gps.year;
            rec.latitude  = last_good_gps.latitude;
            rec.longitude = last_good_gps.longitude;
            rec.altitude  = last_good_gps.has_altitude ? last_good_gps.altitude : 0.0f;
        }

        if (record_number % 10 == 0) {
            printf("[GPS] UART bytes: %lu | Sats: %u | HDOP: %.1f | Fix age: %d\n",
                   (unsigned long)gps_get_bytes_received(),
                   (unsigned)last_good_gps.satellites,
                   last_good_gps.hdop,
                   gps_fix_age);
        }

        if (rec.gps_locked) {
            printf("[Core0] #%lu seq=%lu | %.2f\xc2\xb0""C | GPS: LOCKED%s | "
                   "UTC: %04u-%02u-%02uT%02u:%02u:%02uZ | "
                   "%.5f,%.5f Alt:%.1fm | Sats:%u HDOP:%.1f\n",
                   (unsigned long)record_number,
                   (unsigned long)rec.seq,
                   rec.temperature_c,
                   rec.gps_stale ? " (stale)" : "",
                   rec.year, rec.month, rec.day,
                   rec.hour, rec.minute, rec.second,
                   rec.latitude, rec.longitude,
                   rec.altitude,
                   (unsigned)rec.satellites,
                   rec.hdop);
        } else {
            printf("[Core0] #%lu seq=%lu | %.2f\xc2\xb0""C | GPS: NO LOCK (age:%d, uptime:%.1fs)\n",
                   (unsigned long)record_number,
                   (unsigned long)rec.seq,
                   rec.temperature_c,
                   gps_fix_age,
                   (double)rec.uptime_us / 1000000.0);
        }

        bool added = queue_try_add(&data_queue, &rec);
        if (!added) {
            printf("[Core0] WARNING: Queue full! Record seq=%lu dropped.\n",
                   (unsigned long)rec.seq);
        }

        sleep_until(next_sample);
    }

    return 0;
}
