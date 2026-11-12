#ifndef DTLS_HUE_H
#define DTLS_HUE_H

#include <stdint.h>

#define HUE_MAX_CHANNELS 20

void hue_install_signal_handlers(void);
int hue_should_stop(void);
void hue_request_stop(void);

int hue_dtls_connect(const char *ip, uint16_t port, const char *psk_id, const char *psk_hex);
int hue_dtls_send_rgb16_channels(const char *ec_id_36, uint8_t seq, const uint8_t *channel_ids, const uint16_t *rgb, int num_channels);
int hue_dtls_send_rgb16(const char *ec_id_36, uint8_t seq, const uint16_t *rgb15);
void hue_dtls_close(void);

#endif
