# AGENTS.md — Master Platform Guide & Complete Feature Registry

**Repository:** `hue-dsp`
**Archetype:** `python-uv-ai` — Python `uv` / Native macOS CoreAudio `vDSP` / OpenSSL 3 DTLS 1.2 `HueStream v2.0`
**Primary Stack:** `Python 3.10+ (Pure Stdlib Runtime), Swift 6 (CoreAudio + Accelerate vDSP), C11 (OpenSSL 3 DTLS 1.2 PSK), Ruff, Pytest`
**Initialized:** `2026-11-12` via `sovereign-kb scaffold python-uv-ai`

---

## 🎯 High-Level Agent Operating System

### 1. Autonomous Multi-Model Routing Strategy
| Tier | Role | Target Agent / Tier | Responsibilities |
| :--- | :--- | :--- | :--- |
| **Tier 1** | **Fast Execution & Implementation** | **Fast Worker (`sonnet`)** | Feature/DSP implementation, CLI commands, tests, and rapid fixes. |
| **Tier 2** | **Deep Architecture / Reviewer #1** | **Heavy Worker (`fable`)** | System architecture, CoreAudio/DTLS lifecycle, and documentation coherence. |
| **Tier 3** | **Formal Verification / Reviewer #2** | **Independent Auditor (`astra`)** | Mathematical proofs, C11/Swift concurrency, ASan/TSan bounds, and edge-case audits. |
| **Tier 4** | **Sovereign Local Big-Context** | **Local Long-Context Node** | Private long-context cross-artifact sweeps across code, JSON results, and prose. |

### 2. Mandatory Operating Rules
1. **100% Catalog / Spec Synchronization**: Every feature, CLI command, or native DSP module must be registered in [`docs/FEATURE_CATALOG.md`](docs/FEATURE_CATALOG.md) with its exact implementation path and verification coverage.
2. **Parallel Warm-Started Multi-Model Review (`Rule 1 — Warm-Start, Don't Restrict`)**: Run `fable` and `astra` in parallel (`oc triad "<prompt>"`) with a mechanical Git & test warm-start briefing while keeping full tool access enabled. Never self-exempt code changes as "low-risk" to skip independent review unless explicitly waived by the user.
3. **Zero Private IP or Secret Leaks**: As an open-source repository, `hue-dsp` must contain zero hardcoded private LAN/Tailscale IPs, bridge serials, Entertainment UUIDs, or API tokens (enforced by `test_f02_zero_private_ip_or_secret_leaks`).
4. **Deterministic Quality Gate**: Never declare a task complete until `./scripts/quality_gate.sh` exits `0`.

### 3. Activated Sovereign Agent Skills (`kb skills --archetype python-uv-ai`)
| Skill ID | Local Entry Point (`SKILL.md` + `references/`) |
| :--- | :--- |
| `goldwood-gwi` | `~/.gemini/config/skills/goldwood-gwi/SKILL.md` |
| `gx10-cluster` | `~/.gemini/config/skills/gx10-cluster/SKILL.md` |
| `stochastic-math-econometrics` | `~/.gemini/config/skills/stochastic-math-econometrics/SKILL.md` |
| `web3-fintech-settlement` | `~/.gemini/config/skills/web3-fintech-settlement/SKILL.md` |
| `huggingface-ml` | `~/.gemini/config/skills/huggingface-ml/SKILL.md` |
| `mcp-builder` | `~/.gemini/config/skills/mcp-builder/SKILL.md` |
| `supabase-postgres` | `~/.gemini/config/skills/supabase-postgres/SKILL.md` |
| `principal-engineering` | `~/.gemini/config/skills/principal-engineering/SKILL.md` |

---

## ⚡ Standard Verification Commands

```bash
./scripts/quality_gate.sh
```

---

## 🗺️ Master Traceability Registry (`F-01`–`F-10`)
- `F-01`: Core Architecture & Packaging (`hue-dsp`, `hue-query`, `src/hue_dsp/native/build.sh`)
- `F-02`: OPSEC & Public Sanitization (`test_f02_zero_private_ip_or_secret_leaks`)
- `F-03`: Bridge Discovery & Interactive Pairing (`hue-dsp discover`, `hue-dsp pair`)
- `F-04`: CIE 1931 Color Science & Room Control (`rgb_to_xy`, `parse_color`, `status`, `scenes`, `on`, `off`, `bri`, `color`, `scene`, `flash`)
- `F-05`: C11 Lock-Free Signal & OpenSSL 3 DTLS 1.2 PSK Engine (`dtls_hue.h`, `dtls_hue.c`)
- `F-06`: CoreAudio `CATapDescription` & Bounds-Checked `StereoRingBuffer` (`hue_dsp_stream.swift`)
- `F-07`: Phase-Invariant Dual-Channel `vDSP` 2048-pt FFT (`computeStereoSpectrum`)
- `F-08`: Spectral Flux Onsets, Autocorrelation BPM & Phase-Locked Beat/Bar Oscillators (`BandTracker`, `classifySpatialRole`)
- `F-09`: CoreGraphics Album Artwork Palette & Velvet Noir Modes (`extractPaletteFromArtwork`, `analyze_track_signature`)
- `F-10`: Verified Daemon Lifecycle & CLIP v2 `status == "inactive"` Barrier (`stop_existing_daemon`, `spawn_verified_dsp_daemon`, `_force_stop_entertainment_rest`)

See [`docs/FEATURE_CATALOG.md`](docs/FEATURE_CATALOG.md) for full file and test mappings.
