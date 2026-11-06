#ifndef GPS_PARSER_H
#define GPS_PARSER_H

#include <stdint.h>
#include <stdbool.h>


typedef struct {
    bool     valid;         // true = got a real fix (RMC active AND GGA fix >= 1)
    uint8_t  fix_quality;   // 0=no fix, 1=GPS, 2=DGPS, 6=estimated
    uint8_t  hour;
    uint8_t  minute;
    uint8_t  second;
    uint8_t  day;
    uint8_t  month;
    uint16_t year;
    float    latitude;      // decimal degrees, negative = South
    float    longitude;     // decimal degrees, negative = West
    float    altitude;      // metres above sea level (from GGA)
    bool     has_altitude;  // false if module hasn't reported altitude yet
    uint8_t  satellites;    // satellites in use
    float    hdop;          // horizontal dilution of precision (lower is better)
} gps_data_t;

void     gps_init(void);
void     gps_update(void);
bool     gps_get_data(gps_data_t *out);   // returns true if new fix is available
uint32_t gps_get_bytes_received(void);
uint8_t  gps_get_satellites(void);
bool     gps_is_locked(void);
uint32_t gps_get_baud(void);              // baud the receiver locked onto

#endif
