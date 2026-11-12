"""Comprehensive deterministic test suite for hue-dsp (F-01 through F-10)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import hue_dsp
from hue_dsp import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_DIR = REPO_ROOT / "src" / "hue_dsp" / "native"
SYNTHETIC_BRIDGE_ID = "001788FFFE000000"


def _openssl_flags() -> tuple[str, str]:
    for prefix in ("/opt/homebrew/opt/openssl@3", "/usr/local/opt/openssl@3"):
        if Path(prefix).is_dir():
            return f"-I{prefix}/include", f"-L{prefix}/lib"
    return "", ""


# ---------------------------------------------------------------------------
# F-01: Core Architecture, Packaging & Native Compilation
# ---------------------------------------------------------------------------
def test_f01_packaging_and_cli_help(tmp_path: Path) -> None:
    """Verify F-01 package version, CLI --help, and native C11/Swift 6 compilation."""
    assert hue_dsp.__version__ == "1.0.0"

    res = subprocess.run(
        [sys.executable, "-m", "hue_dsp.cli", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "discover" in res.stdout
    assert "pair" in res.stdout
    assert "velvet" in res.stdout

    out_bin = tmp_path / "hue-dsp-stream"
    build_res = subprocess.run(
        ["/bin/bash", str(NATIVE_DIR / "build.sh"), str(out_bin)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert out_bin.exists() and os.access(out_bin, os.X_OK), build_res.stderr


# ---------------------------------------------------------------------------
# F-02: OPSEC & Zero Private IP / Secret Leak Gate (Scans All Files Incl. Tests)
# ---------------------------------------------------------------------------
def test_f02_zero_private_ip_or_secret_leaks() -> None:
    """Verify F-02: zero private LAN/Tailscale IPs, bridge serials, or tokens across all repo files."""
    rfc1918_and_cgnat = [
        re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
        re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ]
    bridge_eui64_re = re.compile(r"\b(?:001788|ECB5FA)FFFE[0-9A-F]{6}\b", re.IGNORECASE)

    # Dynamically load any locally configured HUE_* values from user's local machine (if present)
    # without hardcoding them in the repository.
    dynamic_secret_literals: set[str] = set()
    for local_cfg in (
        Path.home() / ".config/hue-dsp/config.env",
        Path.home() / ".config/agents/secrets.env",
    ):
        if not local_cfg.exists():
            continue
        for raw in local_cfg.read_text(errors="ignore").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k.strip().startswith("HUE_"):
                    val = v.strip().strip('"').strip("'")
                    if len(val) >= 6 and val not in ("127.0.0.1", "localhost"):
                        dynamic_secret_literals.add(val)

    scan_set: set[Path] = set()
    try:
        git_files = subprocess.check_output(["git", "ls-files"], cwd=REPO_ROOT, text=True, timeout=5).splitlines()
        for gf in git_files:
            p = REPO_ROOT / gf.strip()
            if p.is_file():
                scan_set.add(p.resolve())
    except Exception:
        pass
    for sub in ("src", "tests", "docs", "scripts"):
        d = REPO_ROOT / sub
        if d.exists():
            for p in d.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts:
                    scan_set.add(p.resolve())
    for root_file in ("README.md", "AGENTS.md", "pyproject.toml", "LICENSE", ".gitignore", "uv.lock"):
        rf = REPO_ROOT / root_file
        if rf.exists():
            scan_set.add(rf.resolve())
    scan_paths = sorted(scan_set)

    violations: list[str] = []
    for path in scan_paths:
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in rfc1918_and_cgnat:
            m = pat.search(text)
            if m:
                violations.append(f"{rel}: matched private/CGNAT IP '{m.group(0)}'")
        for m in bridge_eui64_re.finditer(text):
            if m.group(0).upper() != SYNTHETIC_BRIDGE_ID:
                violations.append(f"{rel}: matched bridge EUI-64 '{m.group(0)}'")
        for secret_val in dynamic_secret_literals:
            if secret_val.lower() in text.lower():
                violations.append(f"{rel}: leaked local HUE_* configuration value")

    assert not violations, f"OPSEC leak(s) detected: {violations}"


# ---------------------------------------------------------------------------
# F-03: Bridge Discovery & Interactive Pairing
# ---------------------------------------------------------------------------
def test_f03_bridge_discovery_and_pairing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify F-03 discovery, 0600 atomic config creation, and HUE_* filtering in load_secrets."""
    cfg_file = tmp_path / "config.env"
    cfg_file.write_text('HUE_DEFAULT_GROUP="3"\nUNRELATED_SECRET_TOKEN="should-not-load"\n')
    monkeypatch.setenv("HUE_CONFIG_FILE", str(cfg_file))
    monkeypatch.delenv("HUE_BRIDGE_IP", raising=False)
    monkeypatch.delenv("HUE_APP_KEY", raising=False)
    monkeypatch.delenv("HUE_CLIENT_KEY", raising=False)
    monkeypatch.delenv("UNRELATED_SECRET_TOKEN", raising=False)

    cli.load_secrets()
    assert "UNRELATED_SECRET_TOKEN" not in os.environ
    assert os.environ.get("HUE_DEFAULT_GROUP") == "3"

    def fake_urlopen(req, context=None, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        resp = MagicMock()
        resp.__enter__.return_value = resp
        if "discovery.meethue.com" in url:
            resp.read.return_value = json.dumps(
                [{"id": SYNTHETIC_BRIDGE_ID, "internalipaddress": "198.51.100.42"}]
            ).encode("utf-8")
            return resp
        if url.endswith("/api"):
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["generateclientkey"] is True
            resp.read.return_value = json.dumps(
                [
                    {
                        "success": {
                            "username": "test-app-key-12345",
                            "clientkey": "00112233445566778899AABBCCDDEEFF",
                        }
                    }
                ]
            ).encode("utf-8")
            return resp
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_urlopen)

    bridges = cli.discover_bridges()
    assert len(bridges) >= 1
    assert bridges[0]["ip"] == "198.51.100.42"
    assert bridges[0]["id"] == SYNTHETIC_BRIDGE_ID

    paired = cli.pair_bridge(ip="198.51.100.42", config_path=cfg_file)
    assert paired["username"] == "test-app-key-12345"
    assert paired["clientkey"] == "00112233445566778899AABBCCDDEEFF"
    assert cfg_file.exists()
    mode = cfg_file.stat().st_mode & 0o777
    assert mode == 0o600
    content = cfg_file.read_text()
    assert 'HUE_BRIDGE_IP="198.51.100.42"' in content
    assert 'HUE_APP_KEY="test-app-key-12345"' in content
    assert 'HUE_CLIENT_KEY="00112233445566778899AABBCCDDEEFF"' in content
    assert 'HUE_DEFAULT_GROUP="3"' in content


# ---------------------------------------------------------------------------
# F-04: CIE 1931 Color Science & Color Parser
# ---------------------------------------------------------------------------
def test_f04_cie_xy_color_math_and_parser() -> None:
    """Verify F-04 Wide-RGB D65 CIE 1931 xy transform and color parser."""
    assert cli.rgb_to_xy(0, 0, 0) == [0.3127, 0.3290]

    rx, ry = cli.rgb_to_xy(255, 0, 0)
    gx, gy = cli.rgb_to_xy(0, 255, 0)
    bx, by = cli.rgb_to_xy(0, 0, 255)
    assert rx > 0.65 and ry < 0.35
    assert gy > 0.65
    assert bx < 0.20 and by < 0.10

    assert cli.parse_color("warm") == {"ct": 400}
    assert cli.parse_color("neutral") == {"ct": 300}
    assert cli.parse_color("cool") == {"ct": 175}
    assert cli.parse_color("4000K") == {"ct": 250}
    assert "xy" in cli.parse_color("#ff3366")
    assert "xy" in cli.parse_color("purple")


# ---------------------------------------------------------------------------
# F-05: C11 TSan Atomic Signal Safety & OpenSSL 3 DTLS 1.2 Loopback (1..20 ch)
# ---------------------------------------------------------------------------
def test_f05_c11_tsan_atomic_signal_and_dtls_loopback(tmp_path: Path) -> None:
    """Verify F-05 C11 lock-free atomic signal handler under TSan and DTLS 1.2 PSK 1..20 channel framing."""
    inc_flag, lib_flag = _openssl_flags()
    c_harness = tmp_path / "test_dtls_c11.c"
    c_bin = tmp_path / "test_dtls_c11"

    c_harness.write_text(
        textwrap.dedent(
            """\
            #include "dtls_hue.h"
            #include <arpa/inet.h>
            #include <assert.h>
            #include <netinet/in.h>
            #include <openssl/err.h>
            #include <openssl/ssl.h>
            #include <pthread.h>
            #include <signal.h>
            #include <stdio.h>
            #include <string.h>
            #include <sys/socket.h>
            #include <unistd.h>

            static const char *TEST_PSK_ID = "test-hue-app-id";
            static const char *TEST_PSK_HEX = "00112233445566778899aabbccddeeff";
            static const unsigned char TEST_PSK_RAW[16] = {
                0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
                0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff
            };

            static unsigned int srv_psk_cb(SSL *ssl, const char *identity,
                                           unsigned char *psk, unsigned int max_psk_len) {
                (void)ssl;
                if (!identity || strcmp(identity, TEST_PSK_ID) != 0 || max_psk_len < 16) {
                    return 0;
                }
                memcpy(psk, TEST_PSK_RAW, 16);
                return 16;
            }

            struct server_ctx {
                int fd;
                uint8_t buf5[87];
                uint8_t buf8[108];
                uint8_t buf20[192];
                int n5;
                int n8;
                int n20;
            };

            static void *dtls_server_thread(void *arg) {
                struct server_ctx *s = (struct server_ctx *)arg;
                SSL_CTX *ctx = SSL_CTX_new(DTLS_server_method());
                assert(ctx != NULL);
                SSL_CTX_set_min_proto_version(ctx, DTLS1_2_VERSION);
                SSL_CTX_set_max_proto_version(ctx, DTLS1_2_VERSION);
                SSL_CTX_set_cipher_list(ctx, "PSK-AES128-GCM-SHA256");
                SSL_CTX_set_psk_server_callback(ctx, srv_psk_cb);

                struct sockaddr_in cli_addr;
                socklen_t cli_len = sizeof(cli_addr);
                char peek[1];
                ssize_t pk = recvfrom(s->fd, peek, 1, MSG_PEEK, (struct sockaddr *)&cli_addr, &cli_len);
                assert(pk >= 0);
                assert(connect(s->fd, (struct sockaddr *)&cli_addr, cli_len) == 0);

                SSL *ssl = SSL_new(ctx);
                BIO *bio = BIO_new_dgram(s->fd, BIO_NOCLOSE);
                BIO_ctrl(bio, BIO_CTRL_DGRAM_SET_CONNECTED, 0, &cli_addr);
                SSL_set_bio(ssl, bio, bio);
                assert(SSL_accept(ssl) == 1);

                s->n5 = SSL_read(ssl, s->buf5, sizeof(s->buf5));
                s->n8 = SSL_read(ssl, s->buf8, sizeof(s->buf8));
                s->n20 = SSL_read(ssl, s->buf20, sizeof(s->buf20));

                SSL_shutdown(ssl);
                SSL_free(ssl);
                SSL_CTX_free(ctx);
                return NULL;
            }

            static void *signal_poll_thread(void *arg) {
                (void)arg;
                while (!hue_should_stop()) {
                    usleep(100);
                }
                return NULL;
            }

            int main(void) {
                /* 1. TSan signal concurrency test */
                hue_install_signal_handlers();
                assert(hue_should_stop() == 0);
                pthread_t sig_tid;
                assert(pthread_create(&sig_tid, NULL, signal_poll_thread, NULL) == 0);
                usleep(1000);
                raise(SIGTERM);
                assert(pthread_join(sig_tid, NULL) == 0);
                assert(hue_should_stop() == 1);

                /* 2. Reject invalid hex PSK */
                assert(hue_dtls_connect("127.0.0.1", 2100, TEST_PSK_ID, "00112233445566778899aabbccddeegz") == -2);

                /* 3. Loopback DTLS 1.2 HueStream v2.0 5-channel, 8-channel, and 20-channel framing test */
                struct server_ctx srv;
                memset(&srv, 0, sizeof(srv));
                srv.fd = socket(AF_INET, SOCK_DGRAM, 0);
                assert(srv.fd >= 0);

                struct sockaddr_in addr;
                memset(&addr, 0, sizeof(addr));
                addr.sin_family = AF_INET;
                addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
                addr.sin_port = htons(0);
                assert(bind(srv.fd, (struct sockaddr *)&addr, sizeof(addr)) == 0);

                socklen_t alen = sizeof(addr);
                assert(getsockname(srv.fd, (struct sockaddr *)&addr, &alen) == 0);
                uint16_t port = ntohs(addr.sin_port);

                pthread_t srv_tid;
                assert(pthread_create(&srv_tid, NULL, dtls_server_thread, &srv) == 0);

                assert(hue_dtls_connect("127.0.0.1", port, TEST_PSK_ID, TEST_PSK_HEX) == 0);

                const char *ec_id = "11111111-2222-3333-4444-555555555555";
                uint16_t rgb15[15];
                for (int i = 0; i < 15; i++) rgb15[i] = (uint16_t)(0x1000 + i);
                assert(hue_dtls_send_rgb16(ec_id, 7, rgb15) == 0);

                uint8_t ch8[8] = {0, 1, 2, 3, 4, 5, 6, 7};
                uint16_t rgb24[24];
                for (int i = 0; i < 24; i++) rgb24[i] = (uint16_t)(0x2000 + i);
                assert(hue_dtls_send_rgb16_channels(ec_id, 8, ch8, rgb24, 8) == 0);

                uint8_t ch20[20];
                uint16_t rgb60[60];
                for (int i = 0; i < 20; i++) ch20[i] = (uint8_t)i;
                for (int i = 0; i < 60; i++) rgb60[i] = (uint16_t)(0x3000 + i);
                assert(hue_dtls_send_rgb16_channels(ec_id, 9, ch20, rgb60, 20) == 0);

                /* Out-of-bounds channel count rejection (0 and 21) */
                assert(hue_dtls_send_rgb16_channels(ec_id, 10, ch20, rgb60, 0) == -1);
                assert(hue_dtls_send_rgb16_channels(ec_id, 10, ch20, rgb60, 21) == -1);

                hue_dtls_close();
                assert(pthread_join(srv_tid, NULL) == 0);
                close(srv.fd);

                assert(srv.n5 == 87);
                assert(memcmp(srv.buf5, "HueStream", 9) == 0);
                assert(srv.buf5[9] == 2 && srv.buf5[10] == 0 && srv.buf5[11] == 7);
                assert(memcmp(srv.buf5 + 16, ec_id, 36) == 0);

                assert(srv.n8 == 108);
                assert(srv.buf8[11] == 8);
                assert(srv.buf8[52 + 7 * 7] == 7);

                assert(srv.n20 == 192);
                assert(srv.buf20[11] == 9);
                assert(srv.buf20[52 + 7 * 19] == 19);
                return 0;
            }
            """
        )
    )

    cmd = [
        "/usr/bin/clang",
        "-std=c11",
        "-O1",
        "-g",
        "-fsanitize=thread",
        "-Wall",
        "-Wextra",
        "-Werror",
        f"-I{NATIVE_DIR}",
    ]
    if inc_flag:
        cmd.append(inc_flag)
    if lib_flag:
        cmd.append(lib_flag)
    cmd.extend(
        [
            str(NATIVE_DIR / "dtls_hue.c"),
            str(c_harness),
            "-lssl",
            "-lcrypto",
            "-lpthread",
            "-o",
            str(c_bin),
        ]
    )
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
    run_res = subprocess.run([str(c_bin)], check=True, capture_output=True, text=True, timeout=15)
    assert run_res.returncode == 0


# ---------------------------------------------------------------------------
# F-06, F-07, F-08 & F-09: Swift 6 ASan Planar Bounds, Phase-Invariant FFT,
# BandTracker / Spatial Roles & Artwork Palette Extraction
# ---------------------------------------------------------------------------
def _compile_and_run_swift_probe(tmp_path: Path, swift_test_body: str, sanitize_address: bool = False) -> None:
    inc_flag, lib_flag = _openssl_flags()
    c_obj = tmp_path / "dtls_hue.o"
    c_cmd = [
        "/usr/bin/clang",
        "-std=c11",
        "-O1",
        "-g",
        f"-I{NATIVE_DIR}",
    ]
    if sanitize_address:
        c_cmd.append("-fsanitize=address")
    if inc_flag:
        c_cmd.append(inc_flag)
    c_cmd.extend(["-c", str(NATIVE_DIR / "dtls_hue.c"), "-o", str(c_obj)])
    subprocess.run(c_cmd, check=True, capture_output=True, text=True, timeout=30)

    probe_swift = tmp_path / "probe_main.swift"
    probe_swift.write_text(swift_test_body)
    probe_bin = tmp_path / "probe_bin"

    s_cmd = [
        "/usr/bin/swiftc",
        "-O",
        "-g",
        "-swift-version",
        "6",
        "-D",
        "HUE_DSP_LIBRARY_TEST",
    ]
    if sanitize_address:
        s_cmd.append("-sanitize=address")
    s_cmd.extend(
        [
            str(NATIVE_DIR / "hue_dsp_stream.swift"),
            str(probe_swift),
            str(c_obj),
        ]
    )
    if lib_flag:
        s_cmd.append(lib_flag)
    s_cmd.extend(
        [
            "-lssl",
            "-lcrypto",
            "-framework",
            "CoreAudio",
            "-framework",
            "AudioToolbox",
            "-framework",
            "Accelerate",
            "-framework",
            "CoreGraphics",
            "-framework",
            "ImageIO",
            "-o",
            str(probe_bin),
        ]
    )
    subprocess.run(s_cmd, check=True, capture_output=True, text=True, timeout=60)
    subprocess.run([str(probe_bin)], check=True, capture_output=True, text=True, timeout=20)


def test_f06_swift_asan_planar_bounds_and_ring_buffer(tmp_path: Path) -> None:
    """Verify F-06 planar AudioBufferList bounds checking under AddressSanitizer + guard page."""
    body = textwrap.dedent(
        """\
        import Foundation
        import CoreAudio
        import Darwin

        @main
        enum ProbeF06 {
            static func main() {
                let ring = StereoRingBuffer()
                let pageSize = Int(getpagesize())
                let raw = mmap(nil, pageSize * 2, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0)!
                defer { munmap(raw, pageSize * 2) }
                // Make second page unreadable to trap any OOB read immediately
                mprotect(raw.advanced(by: pageSize), pageSize, PROT_NONE)

                let floatsPerBuf = 64
                let byteSize = floatsPerBuf * MemoryLayout<Float>.size
                let p0 = raw.advanced(by: pageSize - byteSize).assumingMemoryBound(to: Float.self)
                var rArr = [Float](repeating: -0.5, count: floatsPerBuf)
                for i in 0..<floatsPerBuf { p0[i] = 0.5 }

                rArr.withUnsafeMutableBufferPointer { rBuf in
                    let ablSize = MemoryLayout<AudioBufferList>.size + MemoryLayout<AudioBuffer>.size
                    let ablRaw = UnsafeMutableRawPointer.allocate(byteCount: ablSize, alignment: MemoryLayout<AudioBufferList>.alignment)
                    defer { ablRaw.deallocate() }
                    let ablPtr = ablRaw.assumingMemoryBound(to: AudioBufferList.self)
                    ablPtr.pointee.mNumberBuffers = 2
                    let bufs = UnsafeMutableAudioBufferListPointer(ablPtr)
                    // Intentionally pass mNumberChannels = 2 on a 2-buffer list to verify bounds clamping
                    bufs[0] = AudioBuffer(mNumberChannels: 2, mDataByteSize: UInt32(byteSize), mData: UnsafeMutableRawPointer(p0))
                    bufs[1] = AudioBuffer(mNumberChannels: 2, mDataByteSize: UInt32(byteSize), mData: UnsafeMutableRawPointer(rBuf.baseAddress!))
                    ring.ingestAudioBufferList(ablPtr)
                }

                var outL = [Float](repeating: 0, count: 32)
                var outR = [Float](repeating: 0, count: 32)
                ring.readWindow(count: 32, outL: &outL, outR: &outR)
                precondition(abs(outL.last! - 0.5) < 1e-5)
                precondition(abs(outR.last! - (-0.5)) < 1e-5)
            }
        }
        """
    )
    _compile_and_run_swift_probe(tmp_path, body, sanitize_address=True)


def test_f07_phase_invariant_dual_channel_fft(tmp_path: Path) -> None:
    """Verify F-07 dual-channel FFT preserves energy on 180-degree out-of-phase stereo audio."""
    body = textwrap.dedent(
        """\
        import Foundation
        import Accelerate

        @main
        enum ProbeF07 {
            static func main() {
                let fftN = 2048
                let halfN = fftN / 2
                let log2N = vDSP_Length(11)
                let setup = vDSP_create_fftsetup(log2N, FFTRadix(kFFTRadix2))!
                defer { vDSP_destroy_fftsetup(setup) }

                var window = [Float](repeating: 0, count: fftN)
                vDSP_hann_window(&window, vDSP_Length(fftN), Int32(vDSP_HANN_NORM))

                let targetBin = 8
                var winL = [Float](repeating: 0, count: fftN)
                var winR = [Float](repeating: 0, count: fftN)
                for n in 0..<fftN {
                    let s = sin(2.0 * Float.pi * Float(targetBin) * Float(n) / Float(fftN))
                    winL[n] = s
                    winR[n] = -s // 180 deg anti-phase stereo
                }

                var windowed = [Float](repeating: 0, count: fftN)
                var realp = [Float](repeating: 0, count: halfN)
                var imagp = [Float](repeating: 0, count: halfN)
                var magL = [Float](repeating: 0, count: halfN)
                var magR = [Float](repeating: 0, count: halfN)
                var mag = [Float](repeating: 0, count: halfN)

                computeStereoSpectrum(
                    fftSetup: setup,
                    log2N: log2N,
                    fftN: fftN,
                    window: window,
                    winL: winL,
                    winR: winR,
                    windowed: &windowed,
                    realp: &realp,
                    imagp: &imagp,
                    magL: &magL,
                    magR: &magR,
                    mag: &mag
                )
                precondition(mag[targetBin] > 0.35, "Expected non-zero anti-phase magnitude, got \\(mag[targetBin])")
            }
        }
        """
    )
    _compile_and_run_swift_probe(tmp_path, body, sanitize_address=False)


def test_f08_band_tracker_and_spatial_role_classifier(tmp_path: Path) -> None:
    """Verify F-08 BandTracker envelope response and 3D spatial channel role assignment."""
    body = textwrap.dedent(
        """\
        import Foundation

        @main
        enum ProbeF08 {
            static func main() {
                var tracker = BandTracker()
                let quiet = tracker.update(0.001)
                let hit = tracker.update(0.25)
                precondition(hit > quiet && hit <= 1.0)

                precondition(classifySpatialRole(channelId: 0, x: 0.0, y: 0.0, z: 0.0) == .centerKick)
                precondition(classifySpatialRole(channelId: 4, x: -0.09, y: 1.0, z: -1.0) == .frontBass)
                precondition(classifySpatialRole(channelId: 3, x: -0.5, y: 0.1, z: 0.0) == .leftStage)
                precondition(classifySpatialRole(channelId: 2, x: 0.8, y: -0.1, z: 0.0) == .rightStage)
                precondition(classifySpatialRole(channelId: 1, x: 0.2, y: -0.6, z: 0.0) == .rearSnare)

                let mockItem: [String: Any] = [
                    "channels": [
                        ["channel_id": 0, "position": ["x": 0.0, "y": 0.0, "z": 0.0]],
                        ["channel_id": 1, "position": ["x": -0.6, "y": 0.1, "z": 0.0]],
                        ["channel_id": 2, "position": ["x": 0.6, "y": 0.1, "z": 0.0]]
                    ]
                ]
                let chs = parseEntertainmentChannels(from: mockItem)
                precondition(chs.count == 3)
                precondition(chs[0].role == .centerKick)
                precondition(chs[1].role == .leftStage)
                precondition(chs[2].role == .rightStage)
            }
        }
        """
    )
    _compile_and_run_swift_probe(tmp_path, body, sanitize_address=False)


def test_f09_artwork_palette_and_spotify_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify F-09 Velvet Noir / Auto Artwork palette extraction in Swift and Python."""
    body = textwrap.dedent(
        """\
        import Foundation

        @main
        enum ProbeF09 {
            static func main() {
                let velvet = extractPaletteFromArtwork(urlStr: "", velvetMode: true)
                precondition(velvet.count == 4)
                precondition(velvet[0].hex == "#d10a29")

                let stage = extractPaletteFromArtwork(urlStr: "", velvetMode: false)
                precondition(stage.count == 4)

                let state = PaletteState(initialPalette: velvet)
                let snap = state.snapshot()
                precondition(snap.palette.count == 4)
            }
        }
        """
    )
    _compile_and_run_swift_probe(tmp_path, body, sanitize_address=False)

    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("offline")),
    )
    sig = cli.analyze_track_signature("dummytrack", "")
    assert len(sig["palette_hex"]) == 5
    assert len(sig["palette_xy"]) == 5
    assert 60.0 <= sig["bpm"] <= 180.0


# ---------------------------------------------------------------------------
# F-10: Verified Daemon Lifecycle, Fail-Closed PID Identity & CLIP v2 Barrier
# ---------------------------------------------------------------------------
def test_f10_daemon_lifecycle_and_clip_v2_barriers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify F-10 fail-closed PID check, SIGKILL escalation, and CLIP v2 inactive check."""
    pid_file = tmp_path / "hue-test.pid"
    log_file = tmp_path / "hue-test.log"
    monkeypatch.setattr(cli, "PID_FILE", pid_file)
    monkeypatch.setattr(cli, "LOG_FILE", log_file)
    monkeypatch.setenv("HUE_BRIDGE_IP", "127.0.0.1")
    monkeypatch.setenv("HUE_APP_KEY", "test-key")
    monkeypatch.setenv("HUE_CLIENT_KEY", "00112233445566778899aabbccddeeff")

    # 0. Verify _is_hue_daemon_pid fails closed on ps timeout, unrelated cli.py, -c arg spoofing, prefix spoofing, and missing/mismatched lstart
    with (
        patch.object(cli, "_pid_alive", return_value=True),
        patch.object(cli.subprocess, "check_output", side_effect=subprocess.TimeoutExpired("ps", 1.5)),
    ):
        assert cli._is_hue_daemon_pid(424242, expected_lstart="Thu Nov 12 05:00:00 2026") is False

    with (
        patch.object(cli, "_pid_alive", return_value=True),
        patch.object(cli, "_pid_lstart", return_value="Thu Nov 12 05:00:00 2026"),
        patch.object(cli.subprocess, "check_output", return_value="/usr/bin/python3 /unrelated/project/cli.py serve"),
    ):
        assert cli._is_hue_daemon_pid(424242, expected_lstart="Thu Nov 12 05:00:00 2026") is False

    for spoofed_cmd in (
        "/usr/bin/python3 -c 'import time; time.sleep(4)' hue-dsp",
        '/usr/bin/python3 -c"import time; time.sleep(4)" hue-dsp',
        "/usr/bin/python3 -uc 'import time; time.sleep(4)' hue-dsp",
        "/usr/bin/python3 -mhttp.server hue-dsp",
        "/usr/bin/python3 -m http.server hue-dsp",
        "/bin/bash -c 'sleep 4' hue-dsp",
    ):
        with (
            patch.object(cli, "_pid_alive", return_value=True),
            patch.object(cli, "_pid_lstart", return_value="Thu Nov 12 05:00:00 2026"),
            patch.object(cli.subprocess, "check_output", return_value=spoofed_cmd),
        ):
            assert cli._is_hue_daemon_pid(424242, expected_lstart="Thu Nov 12 05:00:00 2026") is False

    with (
        patch.object(cli, "_pid_alive", return_value=True),
        patch.object(cli, "_pid_lstart", return_value="Thu Nov 12 05:00:00 2026"),
        patch.object(cli.subprocess, "check_output", return_value="/unrelated/hue-dsp-stream-backup"),
    ):
        assert cli._is_hue_daemon_pid(424242, expected_lstart="Thu Nov 12 05:00:00 2026") is False

    with (
        patch.object(cli, "_pid_alive", return_value=True),
        patch.object(cli, "_pid_lstart", return_value=None),
        patch.object(cli.subprocess, "check_output", return_value="/opt/bin/hue-dsp-stream"),
    ):
        assert cli._is_hue_daemon_pid(424242, expected_lstart=None) is False

    with (
        patch.object(cli, "_pid_alive", return_value=True),
        patch.object(cli, "_pid_lstart", return_value="Thu Nov 12 05:00:00 2026"),
    ):
        assert cli._is_hue_daemon_pid(424242, expected_lstart="Thu Nov 12 04:00:00 2026") is False

    # Verify PID reuse after SIGTERM does not escalate SIGKILL to the replacement process
    reuse_pid_file = tmp_path / "reuse.pid"
    reuse_pid_file.write_text("424242\nlstart=original-start\n")
    reuse_state = {"reused": False, "killed": False}
    reuse_events: list[tuple[str, str]] = []

    def _mock_alive(_pid: int) -> bool:
        return not reuse_state["killed"]

    def _mock_lstart(_pid: int) -> str:
        return "replacement-start" if reuse_state["reused"] else "original-start"

    def _mock_kill(_pid: int, sig: int) -> None:
        sig_name = "SIGTERM" if sig == cli.signal.SIGTERM else ("SIGKILL" if sig == cli.signal.SIGKILL else str(sig))
        reuse_events.append((sig_name, "replacement" if reuse_state["reused"] else "original"))
        if sig == cli.signal.SIGTERM:
            reuse_state["reused"] = True
        if sig == cli.signal.SIGKILL:
            reuse_state["killed"] = True

    with (
        patch.object(cli, "PID_FILE", reuse_pid_file),
        patch.object(cli, "_pid_alive", side_effect=_mock_alive),
        patch.object(cli, "_pid_lstart", side_effect=_mock_lstart),
        patch.object(cli.subprocess, "check_output", return_value="/opt/bin/hue-dsp-stream"),
        patch.object(cli.os, "kill", side_effect=_mock_kill),
        patch.object(cli, "_force_stop_entertainment_rest"),
        patch.object(cli.time, "sleep"),
    ):
        cli.stop_existing_daemon(timeout_s=0.0)
    assert reuse_events == [("SIGTERM", "original")]

    test_ec = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    clip_calls: list[tuple[str, str, dict | None]] = []

    def fake_clip_ok(ip: str, key: str, method: str, path: str, payload: dict | None = None) -> dict:
        clip_calls.append((method, path, payload))
        if method == "GET" and path == "/clip/v2/resource/entertainment_configuration":
            return {
                "errors": [],
                "data": [{"id": test_ec, "configuration_type": "music", "status": "active"}],
            }
        if method == "PUT" and path == f"/clip/v2/resource/entertainment_configuration/{test_ec}":
            return {
                "errors": [],
                "data": [{"rid": test_ec, "rtype": "entertainment_configuration"}],
            }
        if method == "GET" and path == f"/clip/v2/resource/entertainment_configuration/{test_ec}":
            return {"errors": [], "data": [{"id": test_ec, "status": "inactive"}]}
        raise AssertionError(f"Unexpected CLIP call: {method} {path}")

    monkeypatch.setattr(cli, "_clip_v2_request", fake_clip_ok)

    # 1. Spawn a process that ignores SIGTERM to force SIGKILL escalation + REST inactive barrier
    stub = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ],
        start_new_session=True,
    )
    cli._write_pid_record(stub.pid, test_ec)
    monkeypatch.setattr(
        cli,
        "_is_hue_daemon_pid",
        lambda p, expected_lstart=None, require_lstart=True: p == stub.pid and cli._pid_alive(p),
    )

    stopped, stopped_pid = cli.stop_existing_daemon(timeout_s=0.35)
    assert stopped is True
    assert stopped_pid == stub.pid
    assert not cli._pid_alive(stub.pid)
    assert not pid_file.exists()
    assert (
        "PUT",
        f"/clip/v2/resource/entertainment_configuration/{test_ec}",
        {"action": "stop"},
    ) in clip_calls

    # 2. Verify _force_stop_entertainment_rest raises if status remains "active"
    def fake_clip_stuck_active(ip: str, key: str, method: str, path: str, payload: dict | None = None) -> dict:
        if method == "GET":
            return {
                "errors": [],
                "data": [{"id": test_ec, "configuration_type": "music", "status": "active"}],
            }
        return {"errors": [], "data": [{"rid": test_ec}]}

    monkeypatch.setattr(cli, "_clip_v2_request", fake_clip_stuck_active)
    with pytest.raises(RuntimeError, match="did not report status == 'inactive'"):
        cli._force_stop_entertainment_rest(hint_ec_id=test_ec)

    # 3. Verify spawn_verified_dsp_daemon retains PID_FILE and exits non-zero if startup times out and CLIP cleanup fails
    fake_bin = tmp_path / "hue-dsp-stream-slow"
    fake_bin.write_text("#!/bin/sh\nsleep 30\n")
    fake_bin.chmod(0o755)
    with pytest.raises(SystemExit):
        cli.spawn_verified_dsp_daemon(fake_bin, velvet=False, ready_timeout_s=0.3)
    assert pid_file.exists()
    pid_file.unlink(missing_ok=True)
