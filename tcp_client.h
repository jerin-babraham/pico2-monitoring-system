

#ifndef TCP_CLIENT_H
#define TCP_CLIENT_H

#include <stdbool.h>
#include "gps_temp_monitor.h"  /* sensor_record_t */

/* Max length of one serialized JSON record line (incl. '\n' and NUL). */
#define JSON_LINE_MAX  256


bool tcp_init(void);


int format_sensor_json(char *buf, int buflen, const sensor_record_t *rec);


bool tcp_send_line(const char *line, int len);


void tcp_set_server_ip(const uint8_t ip[4]);


void tcp_log_status(void);

#endif /* TCP_CLIENT_H */
