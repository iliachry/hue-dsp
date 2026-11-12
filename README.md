# `hue-dsp` — Real-Time 48 kHz CoreAudio `vDSP` + 50 Hz DTLS 1.2 Philips Hue Entertainment Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](pyproject.toml)
[![Swift 6](https://img.shields.io/badge/Swift-6.0-F05138.svg)](src/hue_dsp/native/hue_dsp_stream.swift)
[![C11 / OpenSSL 3](https://img.shields.io/badge/C11-OpenSSL%203%20DTLS%201.2-00599C.svg)](src/hue_dsp/native/dtls_hue.c)

**`hue-dsp`** (also invocable as **`hue-query`**) is a zero-dependency macOS command-line controller and real-time audio-reactive lighting engine for **Philips Hue Entertainment**.

Instead of polling slow HTTP REST endpoints or requiring third-party virtual audio loopback drivers, `hue-dsp` taps macOS system audio directly at **48 kHz** via CoreAudio's native `CATapDescription` (`AudioHardwareCreateProcessTap`), computes phase-invariant dual-channel **2048-point Hann-windowed FFTs** via Apple `Accelerate`/`vDSP`, tracks kick/snare transients and autocorrelation tempo (`BPM`) with a phase-locked beat/bar oscillator, and streams 16-bit gamma-corrected RGB frames at **50 Hz** (`20 ms` frame period) over an encrypted **DTLS 1.2** (`PSK-AES128-GCM-SHA256`, UDP port `2100`) **`HueStream v2.0`** channel to `1..20` spatial lights.

---

## ✨ Key Capabilities (`F-01`–`F-10`)

- **Zero-Driver 48 kHz macOS System Audio Tap (`F-06`)**: Captures system-wide stereo output directly from macOS 14.2+ `CoreAudio` (`CATapDescription`) into an `os_unfair_lock`-synchronized, bounds-checked `StereoRingBuffer`.
- **Phase-Invariant Dual-Channel `vDSP` 2048-pt FFT (`F-07`)**: Computes independent Left and Right 2048-sample real FFTs (`23.44 Hz` bin spacing at `48 kHz`) and combines their power spectra $M[k] = \sqrt{0.5(|X_L[k]|^2 + |X_R[k]|^2)}$, eliminating anti-phase stereo cancellation on wide-panned synths and mastered tracks.
- **Spectral Flux Onsets & Phase-Locked Beat/Bar PLL (`F-08`)**: Separates 4 frequency bands (`Sub-Bass 40–120 Hz`, `Bass Groove 120–360 Hz`, `Vocal/Harmonic Mid 360–2800 Hz` with L/R stereo split, and `Brilliance/Air 2.8–11 kHz`), detects half-wave rectified log-spectral flux kick (`40–145 Hz`) and snare/hi-hat (`1.1–7.5 kHz`) transients, and locks a continuous beat/bar oscillator (`65–165 BPM`) via parabolic-interpolated autocorrelation.
- **3D Spatial Channel Synthesis (`1..20` Entertainment Channels) & Artwork Palettes (`F-09`)**: Queries `/clip/v2/resource/entertainment_configuration` to classify each lamp's 3D `(x, y, z)` coordinates into 5 musical roles (*Center Kick Heartbeat*, *Front-Low Radial Bass Wavefront*, *Left Stage Mid + Odd-Beat Sway*, *Right Stage Mid + Even-Beat Sway*, and *Rear Snare/Hi-Hat Backbeat*), driven either by live Spotify album artwork (`CoreGraphics` 12-bin HSV histogram) or the curated **Velvet Noir** (`--velvet`) crimson/ember/burgundy/orchid palette.
- **C11 Lock-Free Signal & OpenSSL 3 DTLS 1.2 PSK Transport (`F-05`)**: Streams `HueStream v2.0` binary frames (`52 + 7 * N` bytes) over UDP `:2100` with async-signal-safe `atomic_int` (`_Static_assert(ATOMIC_INT_LOCK_FREE == 2)`) teardown.
- **Verified Process-Exit & CLIP v2 `status == "inactive"` Barrier (`F-10`)**: Deterministic daemon lifecycle management that confirms both OS process exit and CLIP v2 `status == "inactive"` before releasing or switching modes.

---

## 🚀 Quickstart

### Prerequisites (macOS 14.2+)

1. **Xcode Command Line Tools** (`clang` & `swiftc`) and **OpenSSL 3** (`brew install openssl@3`).
2. **Philips Hue Bridge v2** with an Entertainment Area configured in the official Philips Hue mobile app.

### Installation

```bash
git clone https://github.com/iliachry/hue-dsp.git
cd hue-dsp
uv sync --extra dev
```

### 1. Discover & Pair Your Hue Bridge (`F-03`)

Press the physical **Link button** on top of your Philips Hue Bridge, then run:

```bash
# Discover Hue Bridges on your local network
uv run hue-dsp discover

# Pair and generate both V1 app key and DTLS 1.2 clientkey into ~/.config/hue-dsp/config.env (chmod 600)
uv run hue-dsp pair
```

Alternatively, export or store your credentials in `~/.config/hue-dsp/config.env`:

```ini
HUE_BRIDGE_IP="<bridge_lan_ip>"
HUE_APP_KEY="<hue_v1_username>"
HUE_CLIENT_KEY="<32_hex_char_dtls_psk_clientkey>"
```

### 2. Launch 50 Hz Tempo-Locked Audio Sync

```bash
# Start 50 Hz CoreAudio vDSP + DTLS 1.2 HueStream v2.0 (Auto Spotify Artwork Palette)
uv run hue-dsp start

# Start in Velvet Noir mode (deep crimson, molten ember, burgundy rose & midnight orchid)
uv run hue-dsp velvet

# Inspect live daemon status & real-time BPM / sub-band / centroid telemetry
uv run hue-dsp spotify status

# Stop streaming and verify CLIP v2 Entertainment teardown
uv run hue-dsp stop
```

### 3. Direct Room, Scene & CIE 1931 Color Control (`F-04`)

```bash
# Inspect bridge rooms, groups, and per-lamp state
uv run hue-dsp status

# List all saved scenes on the bridge
uv run hue-dsp scenes

# Activate a scene by name or ID
uv run hue-dsp scene "Arctic aurora"

# Set room or individual lamp color (hex, Kelvin 2000K..6500K, warm/neutral/cool, or named color)
uv run hue-dsp color "#ff3366" --bri 80
uv run hue-dsp color 2700K --light 1

# Set brightness (1-100%) or toggle power
uv run hue-dsp bri 65
uv run hue-dsp on 85
uv run hue-dsp off
```

---

## 🏗️ Repository Layout

```text
hue-dsp/
├── pyproject.toml                       # PEP 621 package metadata & CLI entrypoints (hue-dsp, hue-query)
├── AGENTS.md                            # Agent operating rules & F-01..F-10 traceability matrix
├── LICENSE                              # MIT License
├── docs/
│   ├── ARCHITECTURE.md                  # 48 kHz CoreAudio vDSP + 50 Hz DTLS 1.2 architecture & equations
│   └── FEATURE_CATALOG.md               # Master F-01..F-10 feature catalog & test mappings
├── scripts/
│   └── quality_gate.sh                  # Deterministic 5-stage quality gate (C11/Swift 6 + Ruff + Pytest)
├── src/hue_dsp/
│   ├── __init__.py                      # Package version export
│   ├── cli.py                           # Pure-stdlib CLI, discovery/pairing, CIE xy math & lifecycle barriers
│   └── native/
│       ├── dtls_hue.h                   # C11 DTLS 1.2 PSK & lock-free signal header (1..20 channels)
│       ├── dtls_hue.c                   # OpenSSL 3 DTLS 1.2 PSK client & HueStream v2.0 packet builder
│       ├── hue_dsp_stream.swift         # 48 kHz CoreAudio tap, dual-channel vDSP FFT, BPM PLL & 3D synth
│       └── build.sh                     # Strict C11 (-Werror) + Swift 6 (-strict-concurrency=complete) builder
└── tests/
    └── test_hue_dsp.py                  # 10 deterministic tests (TSan, ASan + guard page, DTLS loopback, FFT)
```

---

## 🧪 Verification & Quality Gate

Run the full 5-stage deterministic quality gate (`10` automated tests in [`tests/test_hue_dsp.py`](tests/test_hue_dsp.py), including C11 ThreadSanitizer signal concurrency, OpenSSL 3 DTLS 1.2 PSK loopback framing, Swift 6 AddressSanitizer planar `AudioBufferList` guard-page bounds, and phase-invariant anti-phase stereo FFT verification):

```bash
./scripts/quality_gate.sh
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/FEATURE_CATALOG.md`](docs/FEATURE_CATALOG.md) for full mathematical and protocol specifications.

---

## 📄 License

MIT License — see [`LICENSE`](LICENSE).
