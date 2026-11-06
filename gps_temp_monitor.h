#ifndef GPS_TEMP_MONITOR_H
#define GPS_TEMP_MONITOR_H

#include <stdint.h>
#include <stdbool.h>


#define DEVICE_ID   "VSSC-GPS-Monitor"
#define FW_VERSION  "1.0"


typedef struct {
    uint64_t uptime_us;       /* Pico uptime since boot */
    float    temperature_c;   /* Internal temperature sensor */
    float    latitude;        /* Decimal degrees, negative = South */
    float    longitude;       /* Decimal degrees, negative = West */
    float    altitude;        /* Metres above sea level */
    float    hdop;            /* Horizontal dilution of precision */
    uint32_t seq;             /* Sequence number, 1-based monotonic */
    uint16_t year;            /* UTC year */
    uint8_t  hour, minute, second, day, month;  /* UTC time/date */
    uint8_t  satellites;      /* Satellites in use */
    uint8_t  fix_quality;     /* GGA fix quality: 0=none, 1=GPS, 2=DGPS */
    bool     gps_locked;      /* Valid GPS fix */
    bool     gps_stale;       /* Using cached fix (no fresh sentence this cycle) */
} sensor_record_t;

#endif
