
#include "gps_parser.h"
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "hardware/uart.h"
#include "hardware/irq.h"
#include "hardware/gpio.h"
#include "pico/time.h"




// UART wiring — the Pico GPIO pins your module's TX/RX are soldered to.
#define GPS_UART        uart1     // UART0 is taken by the debug/USB console
#define GPS_UART_IRQ    UART1_IRQ
#define GPS_PIN_TX      4         // Pico TX → module RX
#define GPS_PIN_RX      5         // Pico RX ← module TX

// Auto-baud detection. With this on, the firmware probes the common baud rates
// at boot and locks onto whichever one the connected module is actually using,
// so you can plug in any NMEA receiver without setting anything by hand.
//  1 detect automatically at startup (adds ~1–6 s to first boot).
//  0 = skip detection and use GPS_BAUD_RATE directly (faster, deterministic).
#define GPS_AUTOBAUD    1

// Serial baud. When GPS_AUTOBAUD = 1 this is only the FALLBACK used if nothing
// is detected; when GPS_AUTOBAUD = 0 it is the fixed rate. Most modules default
// to 9600; some newer u-blox M9/M10 and a few MTK modules ship at 38400/115200.
#define GPS_BAUD_RATE   9600

// u-blox auto-configuration (UBX binary protocol). Keep 0 for any generic or
// non-u-blox module — the parser needs no configuration to work.
// 0 = send nothing; accept whatever NMEA the module emits out of the box.
// 1 = (u-blox ONLY) trim output to GGA+RMC and select Stationary nav mode.

#define GPS_UBLOX_AUTOCONFIG  0

// Ring buffer holding up to one second of UART between gps_update() drains.
// Sized for a chatty module emitting the full NMEA set (GGA/GSA/GSV×n/RMC/
// VTG/GLL) at 9600 baud without dropping bytes. Must stay a power of two.
#define RX_BUF_SIZE     2048


// ring buffer for incoming UART bytes (filled by IRQ, drained by gps_update)
static char              rx_buf[RX_BUF_SIZE];
static volatile uint32_t gps_bytes_received = 0;
static volatile uint16_t rx_head = 0;
static volatile uint16_t rx_tail = 0;

// we keep two copies: pending (being built) and latest (last fully validated fix)
static gps_data_t    latest_gps     = {0};
static gps_data_t    pending_gps    = {0};
static volatile bool new_data_ready = false;

// Commit policy: GGA is the authoritative fix sentence and nearly every
// receiver emits it once per second, so we publish one sample per new second
// as soon as that second's GGA arrives. RMC supplies the date and confirms the
// fix; on the rare module that emits no GGA at all, RMC drives commits instead.
static uint8_t last_commit_second = 0xFF;   // 0xFF = nothing committed yet
static bool    gga_seen_ever      = false;  // does this receiver send GGA?

// RMC carries the date, GGA doesn't — save it separately and merge at commit time
static uint8_t  saved_day   = 0;
static uint8_t  saved_month = 0;
static uint16_t saved_year  = 0;

// scratch buffer for assembling one NMEA sentence at a time
static char    sentence_buf[128];
static uint8_t sentence_idx = 0;

// baud the receiver is actually running at (set by gps_init / auto-baud)
static uint32_t gps_active_baud = GPS_BAUD_RATE;



static void gps_uart_irq_handler(void)
{
    while (uart_is_readable(GPS_UART)) {
        char c = uart_getc(GPS_UART);
        gps_bytes_received++;

        uint16_t next = (rx_head + 1) & (RX_BUF_SIZE - 1);
        if (next != rx_tail) {   // drop byte if buffer is full (shouldn't happen at 9600)
            rx_buf[rx_head] = c;
            rx_head = next;
        }
    }
}



static bool nmea_checksum_valid(const char *sentence)
{
    if (!sentence || sentence[0] != '$') return false;

    const char *star = strchr(sentence + 1, '*');
    if (!star || strlen(star) < 3) return false;

    uint8_t calc = 0;
    for (const char *p = sentence + 1; p < star; p++)
        calc ^= (uint8_t)(*p);

    uint8_t got = (uint8_t)strtol(star + 1, NULL, 16);
    return calc == got;
}



static float nmea_to_decimal(const char *coord)
{
    if (!coord || strlen(coord) < 4) return 0.0f;

    float raw  = (float)atof(coord);
    int   deg  = (int)(raw / 100);
    float mins = raw - (deg * 100);
    return (float)deg + (mins / 60.0f);
}




static int nmea_split(char *buf, char *fields[], int max_fields)
{
    int n = 0;
    fields[n++] = buf;
    for (char *p = buf; *p && n < max_fields; p++) {
        if (*p == ',') {
            *p = '\0';
            fields[n++] = p + 1;
        }
    }
    return n;
}



static void do_commit(bool valid)
{
    // don't publish the same second twice
    if (pending_gps.second == last_commit_second) return;

    // pull date in from RMC (GGA doesn't carry it)
    pending_gps.day   = saved_day;
    pending_gps.month = saved_month;
    pending_gps.year  = saved_year;
    pending_gps.valid = valid;

    latest_gps         = pending_gps;
    new_data_ready     = true;
    last_commit_second = pending_gps.second;
}


static void parse_rmc(const char *sentence)
{
    char buf[128];
    strncpy(buf, sentence, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    // strip checksum and trailing \r
    char *star = strchr(buf, '*');
    if (star) *star = '\0';
    size_t len = strlen(buf);
    if (len > 0 && buf[len - 1] == '\r') buf[len - 1] = '\0';

    // Index fields WITHOUT collapsing empties (see nmea_split). RMC layout:
    //   1=time 2=status 3=lat 4=N/S 5=lon 6=E/W 7=speed 8=course 9=DDMMYY date
    char *f[20];
    int   nf = nmea_split(buf, f, 20);

    bool    active = (nf > 2 && f[2][0] == 'A');
    uint8_t hr = 0, mn = 0, sc = 0;
    float   lat = 0.0f, lon = 0.0f;
    bool    lat_s = false, lon_w = false;
    bool    has_time = false, has_pos = false;

    if (nf > 1 && strlen(f[1]) >= 6) {          // field 1: HHMMSS.ss
        char tmp[3] = {f[1][0], f[1][1], '\0'};
        hr = (uint8_t)atoi(tmp);
        tmp[0] = f[1][2]; tmp[1] = f[1][3]; mn = (uint8_t)atoi(tmp);
        tmp[0] = f[1][4]; tmp[1] = f[1][5]; sc = (uint8_t)atoi(tmp);
        has_time = true;
    }

    if (active && nf > 6) {                      // fields 3-6: position
        if (strlen(f[3]) >= 4) { lat = nmea_to_decimal(f[3]); has_pos = true; }
        if (f[4][0] == 'S') lat_s = true;
        if (strlen(f[5]) >= 4) lon = nmea_to_decimal(f[5]);
        if (f[6][0] == 'W') lon_w = true;
    }

    if (nf > 9 && strlen(f[9]) >= 6) {           // field 9: DDMMYY date
        char tmp[3] = {f[9][0], f[9][1], '\0'};
        uint8_t  d = (uint8_t)atoi(tmp);
        tmp[0] = f[9][2]; tmp[1] = f[9][3]; uint8_t  m = (uint8_t)atoi(tmp);
        tmp[0] = f[9][4]; tmp[1] = f[9][5]; uint16_t y = (uint16_t)atoi(tmp) + 2000;

        // basic sanity check before saving
        if (y >= 2024 && m >= 1 && m <= 12 && d >= 1 && d <= 31) {
            saved_day   = d;
            saved_month = m;
            saved_year  = y;
        }
    }

    if (has_time) {
        pending_gps.hour   = hr;
        pending_gps.minute = mn;
        pending_gps.second = sc;
    }

    if (active && has_pos) {
        pending_gps.latitude  = lat_s ? -lat : lat;
        pending_gps.longitude = lon_w ? -lon : lon;
    }


    if (!gga_seen_ever)
        do_commit(active);
}



static void parse_gga(const char *sentence)
{
    char buf[128];
    strncpy(buf, sentence, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    char *star = strchr(buf, '*');
    if (star) *star = '\0';
    size_t len = strlen(buf);
    if (len > 0 && buf[len - 1] == '\r') buf[len - 1] = '\0';

    // Index fields WITHOUT collapsing empties (see nmea_split). GGA layout:
    //   1=time 2=lat 3=N/S 4=lon 5=E/W 6=fixqual 7=numsats 8=HDOP 9=altitude
    char *f[20];
    int   nf = nmea_split(buf, f, 20);

    uint8_t fix_qual = 0;
    float   lat = 0.0f, lon = 0.0f;
    bool    lat_s = false, lon_w = false, has_pos = false;

    if (nf > 1 && strlen(f[1]) >= 6) {           // field 1: time HHMMSS
        char tmp[3] = {f[1][0], f[1][1], '\0'};
        pending_gps.hour   = (uint8_t)atoi(tmp);
        tmp[0] = f[1][2]; tmp[1] = f[1][3]; pending_gps.minute = (uint8_t)atoi(tmp);
        tmp[0] = f[1][4]; tmp[1] = f[1][5]; pending_gps.second = (uint8_t)atoi(tmp);
    }

    if (nf > 5) {                                 // fields 2-5: position
        if (strlen(f[2]) >= 4) { lat = nmea_to_decimal(f[2]); has_pos = true; }
        if (f[3][0] == 'S') lat_s = true;
        if (strlen(f[4]) >= 4) lon = nmea_to_decimal(f[4]);
        if (f[5][0] == 'W') lon_w = true;
    }

    if (nf > 6) {                                 // field 6: fix quality
        fix_qual = (uint8_t)atoi(f[6]);
        pending_gps.fix_quality = fix_qual;
    }
    if (nf > 7) pending_gps.satellites = (uint8_t)atoi(f[7]);   // field 7
    if (nf > 8 && strlen(f[8]) > 0)                              // field 8: HDOP
        pending_gps.hdop = (float)atof(f[8]);
    if (nf > 9 && fix_qual >= 1 && strlen(f[9]) > 0) {           // field 9: altitude
        pending_gps.altitude     = (float)atof(f[9]);
        pending_gps.has_altitude = true;
    }

    if (fix_qual >= 1 && has_pos) {
        pending_gps.latitude  = lat_s ? -lat : lat;
        pending_gps.longitude = lon_w ? -lon : lon;
    }

    gga_seen_ever = true;
    do_commit(fix_qual >= 1);
}


#if GPS_UBLOX_AUTOCONFIG

// sends a UBX frame: sync + payload + Fletcher checksum
static void ubx_send(const uint8_t *payload, uint16_t len)
{
    uint8_t ck_a = 0, ck_b = 0;

    uart_putc_raw(GPS_UART, 0xB5);  // UBX sync char 1
    uart_putc_raw(GPS_UART, 0x62);  // UBX sync char 2

    for (uint16_t i = 0; i < len; i++) {
        uart_putc_raw(GPS_UART, payload[i]);
        ck_a += payload[i];
        ck_b += ck_a;
    }

    uart_putc_raw(GPS_UART, ck_a);
    uart_putc_raw(GPS_UART, ck_b);
}

// UBX-CFG-MSG: enable/disable a specific NMEA sentence
//   rate=0 → disabled, rate=1 → output on every fix
static void ubx_set_msg_rate(uint8_t msg_class, uint8_t msg_id, uint8_t rate)
{
    uint8_t payload[] = {
        0x06, 0x01,       // class=CFG, id=MSG
        0x03, 0x00,       // payload length = 3
        msg_class, msg_id, rate
    };
    ubx_send(payload, sizeof(payload));
    sleep_ms(50);         // give the module a moment to process
}

// UBX-CFG-NAV5: set navigation engine to Stationary mode for best accuracy
// (only useful if your GPS isn't moving - remove if you need mobile tracking)
static void ubx_set_stationary_mode(void)
{
    uint8_t payload[] = {
        0x06, 0x24,             // CFG-NAV5
        0x24, 0x00,             // 36 bytes follow
        0x01, 0x00,             // mask: only apply dynModel field
        0x02,                   // dynModel = 2 (Stationary)
        0x03,                   // fixMode  = 3 (auto 2D/3D)
        0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x05, 0x00,
        0xFA, 0x00,
        0xFA, 0x00,
        0x64, 0x00,
        0x2C, 0x01,
        0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00,
    };
    ubx_send(payload, sizeof(payload));
    sleep_ms(100);
}

// Turn off sentences we don't use to cut UART traffic by ~60%
// Keeping only GGA and RMC
static void gps_configure_module(void)
{
    printf("[GPS] Sending UBX config to NEO module...\n");

    ubx_set_msg_rate(0xF0, 0x00, 1);  // GGA - keep
    ubx_set_msg_rate(0xF0, 0x04, 1);  // RMC - keep
    ubx_set_msg_rate(0xF0, 0x01, 0);  // GLL - off
    ubx_set_msg_rate(0xF0, 0x02, 0);  // GSA - off
    ubx_set_msg_rate(0xF0, 0x03, 0);  // GSV - off (lots of output, rarely needed)
    ubx_set_msg_rate(0xF0, 0x05, 0);  // VTG - off

    printf("[GPS] Disabled GLL, GSA, GSV, VTG\n");

    ubx_set_stationary_mode();
    printf("[GPS] Nav mode set to Stationary\n");
}

#endif  // GPS_UBLOX_AUTOCONFIG



#if GPS_AUTOBAUD


static const uint32_t baud_candidates[] = { 9600, 38400, 115200, 4800, 57600 };

// Listen this long at each candidate. A 1 Hz receiver emits its whole sentence
// burst once per second, so any window over ~1 s is guaranteed to span a full
// burst (many sentences) when the rate is right.
#define GPS_PROBE_WINDOW_MS  1200u

// Re-sweep the candidate list up to this many times before giving up. A module
// can stay silent for a second or two after power-up, so the first sweep may
// probe the correct rate while the module isn't talking yet; repeating catches
// it once it wakes. Detection returns the instant a rate is confirmed, so a
// module that's present is normally locked on the very first sweep.
#define GPS_AUTOBAUD_PASSES  3

static int gps_probe_baud(uint32_t baud, uint32_t window_ms)
{
    uart_set_baudrate(GPS_UART, baud);

    // Let the new rate settle, then drop any partial frame / noise carried over
    // from the previous rate so counting starts on a clean frame boundary.
    sleep_ms(50);
    while (uart_is_readable(GPS_UART)) (void)uart_getc(GPS_UART);

    char buf[128];
    int  idx   = 0;
    int  valid = 0;
    absolute_time_t deadline = make_timeout_time_ms(window_ms);

    while (!time_reached(deadline)) {
        if (!uart_is_readable(GPS_UART)) continue;

        char c = (char)uart_getc(GPS_UART);
        if (c == '$') {
            idx = 0;
            buf[idx++] = c;
        } else if (c == '\n' && idx > 0) {
            buf[idx] = '\0';
            if (idx >= 6 && buf[0] == '$' && nmea_checksum_valid(buf)) {
                if (++valid >= 2) return valid;   // confident — stop early
            }
            idx = 0;
        } else if (idx > 0 && idx < (int)sizeof(buf) - 1) {
            buf[idx++] = c;
        } else if (idx >= (int)sizeof(buf) - 1) {
            idx = 0;   // overrun — resync on the next '$'
        }
    }
    return valid;
}

// Sweep the candidate rates (re-sweeping a few times so a slow-to-wake module is
// still caught) and return the first that yields valid NMEA, or GPS_BAUD_RATE if
// the module stays silent / unrecognized.
static uint32_t gps_autobaud(void)
{
    printf("[GPS] Auto-detecting baud rate (up to %d passes)...\n",
           GPS_AUTOBAUD_PASSES);

    const size_t n = sizeof(baud_candidates) / sizeof(baud_candidates[0]);
    for (int pass = 1; pass <= GPS_AUTOBAUD_PASSES; pass++) {
        for (size_t i = 0; i < n; i++) {
            uint32_t baud = baud_candidates[i];
            int hits = gps_probe_baud(baud, GPS_PROBE_WINDOW_MS);
            printf("[GPS]   pass %d/%d: %lu baud -> %d valid sentence(s)\n",
                   pass, GPS_AUTOBAUD_PASSES, (unsigned long)baud, hits);
            if (hits >= 2) {
                printf("[GPS] Locked onto %lu baud (pass %d).\n",
                       (unsigned long)baud, pass);
                return baud;
            }
        }
        if (pass < GPS_AUTOBAUD_PASSES)
            printf("[GPS] No NMEA on pass %d — module may still be warming "
                   "up; re-sweeping.\n", pass);
    }

    printf("[GPS] No NMEA after %d passes — falling back to %d baud.\n",
           GPS_AUTOBAUD_PASSES, GPS_BAUD_RATE);
    return GPS_BAUD_RATE;
}

#endif  // GPS_AUTOBAUD


void gps_init(void)
{
  
    uart_init(GPS_UART, GPS_BAUD_RATE);
    gpio_set_function(GPS_PIN_TX, UART_FUNCSEL_NUM(GPS_UART, GPS_PIN_TX));
    gpio_set_function(GPS_PIN_RX, UART_FUNCSEL_NUM(GPS_UART, GPS_PIN_RX));

    uart_set_fifo_enabled(GPS_UART, true);

    // Let the module power up and start emitting NMEA before we probe. Many
    // receivers stay silent for the first second or so after power; probing too
    // early makes auto-baud miss the correct rate and fall back. (The multi-pass
    // sweep is the backstop if a particular module is slower still.)
    sleep_ms(1200);

#if GPS_AUTOBAUD
    uint32_t baud = gps_autobaud();
    uart_set_baudrate(GPS_UART, baud);
#else
    uint32_t baud = GPS_BAUD_RATE;
#endif
    gps_active_baud = baud;

    
    irq_set_exclusive_handler(GPS_UART_IRQ, gps_uart_irq_handler);
    irq_set_enabled(GPS_UART_IRQ, true);
    uart_set_irq_enables(GPS_UART, true, false);

    printf("[GPS] UART1 ready @ %lu baud  RX=GP%d TX=GP%d\n",
           (unsigned long)baud, GPS_PIN_RX, GPS_PIN_TX);
    printf("[GPS] Ring buffer: %d bytes\n", RX_BUF_SIZE);

#if GPS_UBLOX_AUTOCONFIG
    gps_configure_module();   // u-blox only; compiled out otherwise
#else
    printf("[GPS] Generic NMEA mode — using the module's default output.\n");
#endif

    printf("[GPS] Waiting for fix...\n");
}

void gps_update(void)
{
    while (rx_tail != rx_head) {
        char c = rx_buf[rx_tail];
        rx_tail = (rx_tail + 1) & (RX_BUF_SIZE - 1);

        if (c == '$') {
            // start of a new sentence
            sentence_idx = 0;
            sentence_buf[sentence_idx++] = c;

        } else if (c == '\n' && sentence_idx > 0) {
            // end of sentence - try to parse it
            sentence_buf[sentence_idx] = '\0';

           
            if (sentence_idx >= 6 && sentence_buf[0] == '$') {
                const char *type = sentence_buf + 3;
                if (strncmp(type, "RMC", 3) == 0) {
                    if (nmea_checksum_valid(sentence_buf))
                        parse_rmc(sentence_buf);
                } else if (strncmp(type, "GGA", 3) == 0) {
                    if (nmea_checksum_valid(sentence_buf))
                        parse_gga(sentence_buf);
                }
            }

            sentence_idx = 0;

        } else if (sentence_idx > 0 && sentence_idx < (uint8_t)(sizeof(sentence_buf) - 1)) {
            sentence_buf[sentence_idx++] = c;

        } else if (sentence_idx >= sizeof(sentence_buf) - 1) {
            // sentence too long - something went wrong, reset
            sentence_idx = 0;
        }
    }
}


bool gps_get_data(gps_data_t *out)
{
    if (!new_data_ready) return false;
    *out           = latest_gps;
    new_data_ready = false;
    return true;
}

uint32_t gps_get_bytes_received(void) { return gps_bytes_received; }
uint8_t  gps_get_satellites(void)     { return latest_gps.satellites; }
bool     gps_is_locked(void)          { return latest_gps.valid; }
uint32_t gps_get_baud(void)           { return gps_active_baud; }
