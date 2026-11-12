#include "dtls_hue.h"

#include <arpa/inet.h>
#include <ctype.h>
#include <netinet/in.h>
#include <openssl/bio.h>
#include <openssl/crypto.h>
#include <openssl/err.h>
#include <openssl/ssl.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

_Static_assert(ATOMIC_INT_LOCK_FREE == 2, "C11 atomic_int must be lock-free for async-signal-safe stop flag");
static atomic_int g_atomic_stop = 0;

static int g_fd = -1;
static SSL_CTX *g_ctx = NULL;
static SSL *g_ssl = NULL;
static char g_psk_id[128];
static unsigned char g_psk_key[64];
static unsigned int g_psk_key_len = 0;

static int hex_nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return 10 + (c - 'a');
    if (c >= 'A' && c <= 'F') return 10 + (c - 'A');
    return -1;
}

static void hue_sig_handler(int sig) {
    (void)sig;
    atomic_store_explicit(&g_atomic_stop, 1, memory_order_relaxed);
}

void hue_install_signal_handlers(void) {
    atomic_store_explicit(&g_atomic_stop, 0, memory_order_release);
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = hue_sig_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
}

int hue_should_stop(void) {
    return atomic_load_explicit(&g_atomic_stop, memory_order_acquire) != 0 ? 1 : 0;
}

void hue_request_stop(void) {
    atomic_store_explicit(&g_atomic_stop, 1, memory_order_release);
}

static unsigned int psk_client_cb(SSL *ssl, const char *hint,
                                  char *identity, unsigned int max_iden_len,
                                  unsigned char *psk, unsigned int max_psk_len) {
    (void)ssl;
    (void)hint;
    size_t id_len = strlen(g_psk_id);
    if (id_len == 0 || id_len >= (size_t)max_iden_len) {
        return 0;
    }
    memcpy(identity, g_psk_id, id_len + 1);
    if (g_psk_key_len == 0 || g_psk_key_len > max_psk_len) {
        return 0;
    }
    memcpy(psk, g_psk_key, g_psk_key_len);
    return g_psk_key_len;
}

int hue_dtls_connect(const char *ip, uint16_t port, const char *psk_id, const char *psk_hex) {
    if (!ip || !psk_id || !psk_hex) {
        return -1;
    }
    hue_dtls_close();

    size_t id_len = strlen(psk_id);
    if (id_len == 0 || id_len >= sizeof(g_psk_id)) {
        return -1;
    }
    memcpy(g_psk_id, psk_id, id_len + 1);

    size_t hex_len = strlen(psk_hex);
    if (hex_len == 0 || hex_len > (sizeof(g_psk_key) * 2) || (hex_len % 2) != 0) {
        return -1;
    }
    g_psk_key_len = (unsigned int)(hex_len / 2);
    for (unsigned int i = 0; i < g_psk_key_len; i++) {
        int hi = hex_nibble(psk_hex[2 * i]);
        int lo = hex_nibble(psk_hex[2 * i + 1]);
        if (hi < 0 || lo < 0) {
            OPENSSL_cleanse(g_psk_key, sizeof(g_psk_key));
            g_psk_key_len = 0;
            return -2;
        }
        g_psk_key[i] = (unsigned char)((hi << 4) | lo);
    }

    g_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (g_fd < 0) {
        hue_dtls_close();
        return -3;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (inet_pton(AF_INET, ip, &addr.sin_addr) != 1) {
        hue_dtls_close();
        return -4;
    }

    if (connect(g_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        hue_dtls_close();
        return -5;
    }

    struct timeval tv_rcv = { .tv_sec = 2, .tv_usec = 500000 };
    struct timeval tv_snd = { .tv_sec = 0, .tv_usec = 500000 };
    setsockopt(g_fd, SOL_SOCKET, SO_RCVTIMEO, &tv_rcv, sizeof(tv_rcv));
    setsockopt(g_fd, SOL_SOCKET, SO_SNDTIMEO, &tv_snd, sizeof(tv_snd));

    g_ctx = SSL_CTX_new(DTLS_client_method());
    if (!g_ctx) {
        hue_dtls_close();
        return -6;
    }

    SSL_CTX_set_min_proto_version(g_ctx, DTLS1_2_VERSION);
    SSL_CTX_set_max_proto_version(g_ctx, DTLS1_2_VERSION);
    SSL_CTX_set_cipher_list(g_ctx, "PSK-AES128-GCM-SHA256");
    SSL_CTX_set_psk_client_callback(g_ctx, psk_client_cb);

    g_ssl = SSL_new(g_ctx);
    if (!g_ssl) {
        hue_dtls_close();
        return -7;
    }

    BIO *bio = BIO_new_dgram(g_fd, BIO_NOCLOSE);
    if (!bio) {
        hue_dtls_close();
        return -8;
    }
    BIO_ctrl(bio, BIO_CTRL_DGRAM_SET_CONNECTED, 0, &addr);
    SSL_set_bio(g_ssl, bio, bio);

    if (SSL_connect(g_ssl) != 1) {
        ERR_print_errors_fp(stderr);
        hue_dtls_close();
        return -9;
    }

    return 0;
}

int hue_dtls_send_rgb16_channels(const char *ec_id_36, uint8_t seq, const uint8_t *channel_ids, const uint16_t *rgb, int num_channels) {
    if (!g_ssl || !ec_id_36 || !rgb || num_channels < 1 || num_channels > HUE_MAX_CHANNELS) {
        return -1;
    }
    if (strnlen(ec_id_36, 37) != 36) {
        return -1;
    }
    uint8_t pkt[52 + 7 * HUE_MAX_CHANNELS];
    memcpy(pkt, "HueStream", 9);
    pkt[9] = 2;   /* Major v2 */
    pkt[10] = 0;  /* Minor v0 */
    pkt[11] = seq;
    pkt[12] = 0;
    pkt[13] = 0;
    pkt[14] = 0;  /* Color space 0 = RGB 16-bit */
    pkt[15] = 0;
    memcpy(pkt + 16, ec_id_36, 36);

    for (int ch = 0; ch < num_channels; ch++) {
        uint16_t r = rgb[ch * 3 + 0];
        uint16_t g = rgb[ch * 3 + 1];
        uint16_t b = rgb[ch * 3 + 2];
        int off = 52 + ch * 7;
        pkt[off + 0] = channel_ids ? channel_ids[ch] : (uint8_t)ch;
        pkt[off + 1] = (uint8_t)((r >> 8) & 0xFF);
        pkt[off + 2] = (uint8_t)(r & 0xFF);
        pkt[off + 3] = (uint8_t)((g >> 8) & 0xFF);
        pkt[off + 4] = (uint8_t)(g & 0xFF);
        pkt[off + 5] = (uint8_t)((b >> 8) & 0xFF);
        pkt[off + 6] = (uint8_t)(b & 0xFF);
    }

    int pkt_len = 52 + num_channels * 7;
    int written = SSL_write(g_ssl, pkt, pkt_len);
    return (written == pkt_len) ? 0 : -2;
}

int hue_dtls_send_rgb16(const char *ec_id_36, uint8_t seq, const uint16_t *rgb15) {
    return hue_dtls_send_rgb16_channels(ec_id_36, seq, NULL, rgb15, 5);
}

void hue_dtls_close(void) {
    if (g_ssl) {
        SSL_shutdown(g_ssl);
        SSL_free(g_ssl);
        g_ssl = NULL;
    }
    if (g_ctx) {
        SSL_CTX_free(g_ctx);
        g_ctx = NULL;
    }
    if (g_fd >= 0) {
        close(g_fd);
        g_fd = -1;
    }
    OPENSSL_cleanse(g_psk_id, sizeof(g_psk_id));
    OPENSSL_cleanse(g_psk_key, sizeof(g_psk_key));
    g_psk_key_len = 0;
}
