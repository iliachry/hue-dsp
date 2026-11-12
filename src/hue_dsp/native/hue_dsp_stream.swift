import Foundation
import CoreAudio
import AudioToolbox
import Accelerate
import ImageIO
import os

// MARK: - C DTLS & Signal Bridge Declarations
@_silgen_name("hue_install_signal_handlers")
func c_hue_install_signal_handlers()

@_silgen_name("hue_should_stop")
func c_hue_should_stop() -> Int32

@_silgen_name("hue_request_stop")
func c_hue_request_stop()

@_silgen_name("hue_dtls_connect")
func c_hue_dtls_connect(_ ip: UnsafePointer<CChar>, _ port: UInt16, _ pskId: UnsafePointer<CChar>, _ pskHex: UnsafePointer<CChar>) -> Int32

@_silgen_name("hue_dtls_send_rgb16_channels")
func c_hue_dtls_send_rgb16_channels(
    _ ecId: UnsafePointer<CChar>,
    _ seq: UInt8,
    _ channelIds: UnsafePointer<UInt8>?,
    _ rgb: UnsafePointer<UInt16>,
    _ numChannels: Int32
) -> Int32

@_silgen_name("hue_dtls_send_rgb16")
func c_hue_dtls_send_rgb16(_ ecId: UnsafePointer<CChar>, _ seq: UInt8, _ rgb15: UnsafePointer<UInt16>) -> Int32

@_silgen_name("hue_dtls_close")
func c_hue_dtls_close()

// MARK: - Types & Math Helpers
struct RGB: Sendable {
    var r: Float
    var g: Float
    var b: Float

    static func lerp(_ a: RGB, _ b: RGB, _ t: Float) -> RGB {
        let u = max(0.0, min(1.0, t))
        return RGB(r: a.r + (b.r - a.r) * u, g: a.g + (b.g - a.g) * u, b: a.b + (b.b - a.b) * u)
    }

    func scaled(_ s: Float) -> RGB {
        return RGB(r: r * s, g: g * s, b: b * s)
    }

    var hex: String {
        let ri = max(0, min(255, Int(round(r * 255.0))))
        let gi = max(0, min(255, Int(round(g * 255.0))))
        let bi = max(0, min(255, Int(round(b * 255.0))))
        return String(format: "#%02x%02x%02x", ri, gi, bi)
    }
}

enum SpatialRole: Int, Sendable {
    case centerKick = 0
    case rearSnare = 1
    case rightStage = 2
    case leftStage = 3
    case frontBass = 4
}

struct EntertainmentChannel: Sendable {
    let channelId: UInt8
    let x: Float
    let y: Float
    let z: Float
    let role: SpatialRole
}

struct EntertainmentZone: Sendable {
    let id: String
    let channels: [EntertainmentChannel]
}

@inline(__always)
func clamp01(_ x: Float) -> Float {
    return max(0.0, min(1.0, x))
}

@inline(__always)
func mod1(_ x: Float) -> Float {
    let r = x - floor(x)
    return r < 0 ? r + 1.0 : r
}

func rgbToHsv(_ r: Float, _ g: Float, _ b: Float) -> (h: Float, s: Float, v: Float) {
    let mx = max(r, max(g, b))
    let mn = min(r, min(g, b))
    let d = mx - mn
    var h: Float = 0
    let s: Float = mx <= 1e-6 ? 0 : d / mx
    if d > 1e-6 {
        if mx == r {
            h = (g - b) / d + (g < b ? 6.0 : 0.0)
        } else if mx == g {
            h = (b - r) / d + 2.0
        } else {
            h = (r - g) / d + 4.0
        }
        h /= 6.0
    }
    return (h, s, mx)
}

func hsvToRgb(_ h: Float, _ s: Float, _ v: Float) -> RGB {
    let hh = mod1(h) * 6.0
    let i = Int(floor(hh))
    let f = hh - Float(i)
    let p = v * (1.0 - s)
    let q = v * (1.0 - s * f)
    let t = v * (1.0 - s * (1.0 - f))
    switch i % 6 {
    case 0: return RGB(r: v, g: t, b: p)
    case 1: return RGB(r: q, g: v, b: p)
    case 2: return RGB(r: p, g: v, b: t)
    case 3: return RGB(r: p, g: q, b: v)
    case 4: return RGB(r: t, g: p, b: v)
    default: return RGB(r: v, g: p, b: q)
    }
}

func classifySpatialRole(channelId: UInt8, x: Float, y: Float, z: Float) -> SpatialRole {
    let radialXY = sqrt(x * x + y * y)
    if radialXY < 0.18 && abs(z) < 0.25 {
        return .centerKick
    }
    if y > 0.45 || z < -0.45 {
        return .frontBass
    }
    if x < -0.15 {
        return .leftStage
    }
    if y < -0.35 {
        return .rearSnare
    }
    if x > 0.15 {
        return .rightStage
    }
    return SpatialRole(rawValue: Int(channelId) % 5) ?? .centerKick
}

// MARK: - Synchronized Palette & Track State
final class PaletteState: @unchecked Sendable {
    private let lock = NSLock()
    private var palette: [RGB]
    private var trackLabel: String

    init(initialPalette: [RGB], initialLabel: String = "System Audio") {
        self.palette = initialPalette
        self.trackLabel = initialLabel
    }

    func snapshot() -> (palette: [RGB], trackLabel: String) {
        lock.lock()
        defer { lock.unlock() }
        return (palette, trackLabel)
    }

    func update(palette newPalette: [RGB], trackLabel newLabel: String) {
        lock.lock()
        self.palette = newPalette
        self.trackLabel = newLabel
        lock.unlock()
    }
}

private final class DataBox: @unchecked Sendable {
    var data = Data()
}

// MARK: - Configuration & Bridge Helpers
func loadSecrets() -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    let home = NSHomeDirectory() as NSString
    let candidatePaths = [
        env["HUE_CONFIG_FILE"] ?? "",
        home.appendingPathComponent(".config/hue-dsp/config.env"),
        home.appendingPathComponent(".config/agents/secrets.env")
    ]
    for path in candidatePaths where !path.isEmpty {
        guard let content = try? String(contentsOfFile: path, encoding: .utf8) else { continue }
        for raw in content.components(separatedBy: .newlines) {
            var line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            if line.hasPrefix("export ") {
                line = String(line.dropFirst(7)).trimmingCharacters(in: .whitespaces)
            }
            if let eq = line.firstIndex(of: "=") {
                let k = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
                guard k.hasPrefix("HUE_") else { continue }
                var v = String(line[line.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
                if (v.hasPrefix("\"") && v.hasSuffix("\"")) || (v.hasPrefix("'") && v.hasSuffix("'")) {
                    v = String(v.dropFirst().dropLast())
                }
                if env[k] == nil || env[k]?.isEmpty == true {
                    env[k] = v
                }
            }
        }
    }
    return env
}

func runProcess(_ launchPath: String, _ args: [String], timeoutSec: Double = 4.0, abortOnStop: Bool = false) -> String {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: launchPath)
    p.arguments = args
    let outPipe = Pipe()
    p.standardOutput = outPipe
    p.standardError = FileHandle.nullDevice
    p.standardInput = FileHandle.nullDevice
    do {
        try p.run()
    } catch {
        return ""
    }

    let box = DataBox()
    let readGroup = DispatchGroup()
    readGroup.enter()
    let readHandle = outPipe.fileHandleForReading
    DispatchQueue.global(qos: .utility).async {
        box.data = readHandle.readDataToEndOfFile()
        readGroup.leave()
    }

    let deadline = Date().addingTimeInterval(timeoutSec)
    while p.isRunning && Date() < deadline {
        if abortOnStop && c_hue_should_stop() != 0 {
            p.terminate()
            _ = readGroup.wait(timeout: .now() + 0.2)
            return ""
        }
        Thread.sleep(forTimeInterval: 0.02)
    }
    if p.isRunning {
        p.terminate()
        _ = readGroup.wait(timeout: .now() + 0.2)
        return ""
    }
    guard readGroup.wait(timeout: .now() + 0.5) == .success else {
        return ""
    }
    return String(data: box.data, encoding: .utf8) ?? ""
}

func fetchHuePskIdentity(ip: String, appKey: String) -> String? {
    let out = runProcess("/usr/bin/curl", ["-ski", "-m", "3", "-H", "hue-application-key: \(appKey)", "https://\(ip)/auth/v1"])
    for line in out.components(separatedBy: .newlines) {
        let lower = line.lowercased()
        if lower.hasPrefix("hue-application-id:") {
            let val = line.dropFirst("hue-application-id:".count).trimmingCharacters(in: .whitespacesAndNewlines)
            if !val.isEmpty { return val }
        }
    }
    return nil
}

func defaultFiveChannels() -> [EntertainmentChannel] {
    return [
        EntertainmentChannel(channelId: 0, x: 0.00, y: 0.00, z: 0.00, role: .centerKick),
        EntertainmentChannel(channelId: 1, x: 0.31, y: -0.61, z: 0.00, role: .rearSnare),
        EntertainmentChannel(channelId: 2, x: 0.93, y: -0.23, z: 0.00, role: .rightStage),
        EntertainmentChannel(channelId: 3, x: -0.33, y: 0.13, z: 0.00, role: .leftStage),
        EntertainmentChannel(channelId: 4, x: -0.09, y: 1.00, z: -1.00, role: .frontBass)
    ]
}

func parseEntertainmentChannels(from item: [String: Any]) -> [EntertainmentChannel] {
    guard let rawChannels = item["channels"] as? [[String: Any]], !rawChannels.isEmpty else {
        return defaultFiveChannels()
    }
    var parsed: [EntertainmentChannel] = []
    for raw in rawChannels.prefix(20) {
        guard let cidInt = raw["channel_id"] as? Int, (0...255).contains(cidInt) else { continue }
        let cid = UInt8(cidInt)
        let pos = raw["position"] as? [String: Any]
        let x = Float((pos?["x"] as? NSNumber)?.doubleValue ?? 0.0)
        let y = Float((pos?["y"] as? NSNumber)?.doubleValue ?? 0.0)
        let z = Float((pos?["z"] as? NSNumber)?.doubleValue ?? 0.0)
        let role = classifySpatialRole(channelId: cid, x: x, y: y, z: z)
        parsed.append(EntertainmentChannel(channelId: cid, x: x, y: y, z: z, role: role))
    }
    return parsed.isEmpty ? defaultFiveChannels() : parsed
}

func resolveEntertainmentZone(ip: String, appKey: String, preferredId: String? = nil) -> EntertainmentZone? {
    let raw = runProcess("/usr/bin/curl", ["-sk", "-m", "3", "-H", "hue-application-key: \(appKey)", "https://\(ip)/clip/v2/resource/entertainment_configuration"])
    if let data = raw.data(using: .utf8),
       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let items = obj["data"] as? [[String: Any]], !items.isEmpty {
        if let pref = preferredId, pref.count == 36,
           let matched = items.first(where: { ($0["id"] as? String) == pref }) {
            return EntertainmentZone(id: pref, channels: parseEntertainmentChannels(from: matched))
        }
        if let music = items.first(where: { ($0["configuration_type"] as? String) == "music" }),
           let id = music["id"] as? String, id.count == 36 {
            return EntertainmentZone(id: id, channels: parseEntertainmentChannels(from: music))
        }
        if let id = items[0]["id"] as? String, id.count == 36 {
            return EntertainmentZone(id: id, channels: parseEntertainmentChannels(from: items[0]))
        }
    }
    if let pref = preferredId, pref.count == 36 {
        return EntertainmentZone(id: pref, channels: defaultFiveChannels())
    }
    return nil
}

@discardableResult
func setEntertainmentStreaming(ip: String, appKey: String, ecId: String, active: Bool) -> Bool {
    let body = active ? "{\"action\":\"start\"}" : "{\"action\":\"stop\"}"
    let attempts = active ? 1 : 2
    for _ in 0..<attempts {
        let out = runProcess("/usr/bin/curl", [
            "-sk", "-m", "3", "-w", "\n%{http_code}", "-X", "PUT",
            "-H", "hue-application-key: \(appKey)",
            "-H", "Content-Type: application/json",
            "-d", body,
            "https://\(ip)/clip/v2/resource/entertainment_configuration/\(ecId)"
        ])
        let lines = out.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: .newlines)
        guard let codeStr = lines.last, let code = Int(codeStr), (200...299).contains(code) else {
            continue
        }
        let jsonPart = lines.dropLast().joined(separator: "\n")
        guard let data = jsonPart.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let errs = obj["errors"] as? [Any], errs.isEmpty else {
            continue
        }
        if active {
            return true
        }
        // Require explicit status == "inactive" confirmation on the matching entertainment_configuration
        for _ in 0..<8 {
            let stOut = runProcess("/usr/bin/curl", [
                "-sk", "-m", "3", "-w", "\n%{http_code}", "-X", "GET",
                "-H", "hue-application-key: \(appKey)",
                "https://\(ip)/clip/v2/resource/entertainment_configuration/\(ecId)"
            ])
            let stLines = stOut.trimmingCharacters(in: .whitespacesAndNewlines).components(separatedBy: .newlines)
            if let stCodeStr = stLines.last, let stCode = Int(stCodeStr), (200...299).contains(stCode) {
                let stJson = stLines.dropLast().joined(separator: "\n")
                if let stData = stJson.data(using: .utf8),
                   let stObj = try? JSONSerialization.jsonObject(with: stData) as? [String: Any],
                   let stErrs = stObj["errors"] as? [Any], stErrs.isEmpty,
                   let items = stObj["data"] as? [[String: Any]],
                   items.contains(where: { ($0["id"] as? String) == ecId && ($0["status"] as? String) == "inactive" }) {
                    return true
                }
            }
            Thread.sleep(forTimeInterval: 0.08)
        }
    }
    return false
}

// MARK: - Artwork Palette Extractor (CoreGraphics)
let defaultVelvetPalette: [RGB] = [
    RGB(r: 0.82, g: 0.04, b: 0.16), // C0: Velvet Crimson (Kick / Center)
    RGB(r: 1.00, g: 0.30, b: 0.05), // C1: Molten Ember (Bass / Groove)
    RGB(r: 0.72, g: 0.04, b: 0.38), // C2: Burgundy Rose (Melodic Lead)
    RGB(r: 0.44, g: 0.06, b: 0.78)  // C3: Midnight Orchid (High / Sparkle)
]

let defaultStagePalette: [RGB] = [
    RGB(r: 0.92, g: 0.08, b: 0.22),
    RGB(r: 1.00, g: 0.42, b: 0.06),
    RGB(r: 0.08, g: 0.52, b: 1.00),
    RGB(r: 0.78, g: 0.08, b: 0.95)
]

func extractPaletteFromArtwork(urlStr: String, velvetMode: Bool) -> [RGB] {
    if velvetMode {
        return defaultVelvetPalette
    }
    guard !urlStr.isEmpty,
          let url = URL(string: urlStr),
          let imgData = try? Data(contentsOf: url),
          let src = CGImageSourceCreateWithData(imgData as CFData, nil),
          let cgImg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        return defaultStagePalette
    }

    let w = 24, h = 24
    var pixels = [UInt8](repeating: 0, count: w * h * 4)
    let cs = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(
        data: &pixels,
        width: w,
        height: h,
        bitsPerComponent: 8,
        bytesPerRow: w * 4,
        space: cs,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return defaultStagePalette
    }
    ctx.draw(cgImg, in: CGRect(x: 0, y: 0, width: w, height: h))

    var buckets = Array(repeating: [(score: Float, h: Float, s: Float, v: Float)](), count: 12)
    for i in 0..<(w * h) {
        let r = Float(pixels[i * 4 + 0]) / 255.0
        let g = Float(pixels[i * 4 + 1]) / 255.0
        let b = Float(pixels[i * 4 + 2]) / 255.0
        let hsv = rgbToHsv(r, g, b)
        if hsv.v < 0.16 { continue }
        let bIdx = Int(hsv.h * 12.0) % 12
        let score = (hsv.s + 0.22) * (hsv.v + 0.18)
        buckets[bIdx].append((score, hsv.h, hsv.s, hsv.v))
    }

    let ranked = buckets.filter { !$0.isEmpty }.sorted {
        $0.reduce(0.0) { $0 + $1.score } > $1.reduce(0.0) { $0 + $1.score }
    }

    var chosenHues: [Float] = []
    var result: [RGB] = []
    for grp in ranked {
        guard let best = grp.max(by: { $0.score < $1.score }) else { continue }
        let distinct = chosenHues.allSatisfy { sh in
            let d = abs(best.h - sh)
            return min(d, 1.0 - d) > 0.11
        }
        if distinct || result.isEmpty {
            chosenHues.append(best.h)
            let vividS = max(0.80, min(1.0, best.s + 0.45))
            result.append(hsvToRgb(best.h, vividS, 1.0))
        }
        if result.count == 4 { break }
    }

    let baseH = chosenHues.first ?? 0.04
    let offsets: [Float] = [0.0, 0.14, 0.56, 0.78]
    while result.count < 4 {
        let nh = mod1(baseH + offsets[result.count])
        result.append(hsvToRgb(nh, 0.90, 1.0))
    }
    return result
}

// MARK: - Real-Time Safe Stereo Audio Ring Buffer (Bounds-Checked + os_unfair_lock)
final class StereoRingBuffer: @unchecked Sendable {
    let capacity = 8192
    let mask = 8191
    private var left: [Float]
    private var right: [Float]
    private var writeIdx: Int = 0
    private let lockPtr: UnsafeMutablePointer<os_unfair_lock>

    init() {
        left = [Float](repeating: 0, count: capacity)
        right = [Float](repeating: 0, count: capacity)
        lockPtr = UnsafeMutablePointer<os_unfair_lock>.allocate(capacity: 1)
        lockPtr.initialize(to: os_unfair_lock())
    }

    deinit {
        lockPtr.deinitialize(count: 1)
        lockPtr.deallocate()
    }

    func pushInterleaved(_ ptr: UnsafePointer<Float>, frames: Int, channels: Int) {
        guard frames > 0, channels >= 1 else { return }
        os_unfair_lock_lock(lockPtr)
        var wi = writeIdx
        if channels >= 2 {
            for f in 0..<frames {
                left[wi] = ptr[f * channels]
                right[wi] = ptr[f * channels + 1]
                wi = (wi + 1) & mask
            }
        } else {
            for f in 0..<frames {
                let m = ptr[f]
                left[wi] = m
                right[wi] = m
                wi = (wi + 1) & mask
            }
        }
        writeIdx = wi
        os_unfair_lock_unlock(lockPtr)
    }

    func pushPlanar(_ lPtr: UnsafePointer<Float>, stride0: Int, _ rPtr: UnsafePointer<Float>, stride1: Int, frames: Int) {
        guard frames > 0, stride0 >= 1, stride1 >= 1 else { return }
        os_unfair_lock_lock(lockPtr)
        var wi = writeIdx
        for f in 0..<frames {
            left[wi] = lPtr[f * stride0]
            right[wi] = rPtr[f * stride1]
            wi = (wi + 1) & mask
        }
        writeIdx = wi
        os_unfair_lock_unlock(lockPtr)
    }

    func ingestAudioBufferList(_ inInputData: UnsafePointer<AudioBufferList>) {
        let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))
        if abl.count >= 2 {
            guard let d0 = abl[0].mData, let d1 = abl[1].mData else { return }
            let ch0 = max(1, Int(abl[0].mNumberChannels))
            let ch1 = max(1, Int(abl[1].mNumberChannels))
            let floats0 = Int(abl[0].mDataByteSize) / MemoryLayout<Float>.size
            let floats1 = Int(abl[1].mDataByteSize) / MemoryLayout<Float>.size
            let frames = min(floats0 / ch0, floats1 / ch1)
            guard frames > 0 else { return }
            let p0 = d0.assumingMemoryBound(to: Float.self)
            let p1 = d1.assumingMemoryBound(to: Float.self)
            pushPlanar(p0, stride0: ch0, p1, stride1: ch1, frames: frames)
        } else if abl.count == 1 {
            guard let d0 = abl[0].mData else { return }
            let ch = max(1, Int(abl[0].mNumberChannels))
            let totalFloats = Int(abl[0].mDataByteSize) / MemoryLayout<Float>.size
            let frames = totalFloats / ch
            guard frames > 0 else { return }
            let p0 = d0.assumingMemoryBound(to: Float.self)
            pushInterleaved(p0, frames: frames, channels: ch)
        }
    }

    func readWindow(count: Int, outL: inout [Float], outR: inout [Float]) {
        let n = min(count, min(outL.count, outR.count))
        guard n > 0 else { return }
        os_unfair_lock_lock(lockPtr)
        let end = writeIdx
        var idx = (end - n) & mask
        for i in 0..<n {
            outL[i] = left[idx]
            outR[i] = right[idx]
            idx = (idx + 1) & mask
        }
        os_unfair_lock_unlock(lockPtr)
    }
}

// MARK: - Contrast-Expanded Sub-Band Envelope Follower
struct BandTracker {
    var fast: Float = 0.0
    var floorVal: Float = 0.002
    var peakVal: Float = 0.015

    mutating func update(_ raw: Float) -> Float {
        if raw > fast {
            fast += 0.72 * (raw - fast) // ~16ms crisp attack
        } else {
            fast += 0.16 * (raw - fast) // ~110ms musical decay
        }
        floorVal += 0.016 * (raw - floorVal)
        if fast > peakVal {
            peakVal = fast
        } else {
            peakVal = max(floorVal + 0.004, peakVal * 0.9955)
        }
        let base = 0.54 * floorVal
        let span = max(0.003, peakVal - base)
        let norm = clamp01((fast - base) / span)
        return pow(norm, 1.30)
    }
}

// MARK: - Phase-Robust Dual-Channel Power Spectrum Helper
func computeStereoSpectrum(
    fftSetup: FFTSetup,
    log2N: vDSP_Length,
    fftN: Int,
    window: [Float],
    winL: [Float],
    winR: [Float],
    windowed: inout [Float],
    realp: inout [Float],
    imagp: inout [Float],
    magL: inout [Float],
    magR: inout [Float],
    mag: inout [Float]
) {
    let halfN = fftN / 2
    var scale = 1.0 / Float(fftN)

    func channelMag(_ input: [Float], _ outMag: inout [Float]) {
        vDSP_vmul(input, 1, window, 1, &windowed, 1, vDSP_Length(fftN))
        realp.withUnsafeMutableBufferPointer { rBuf in
            imagp.withUnsafeMutableBufferPointer { iBuf in
                var split = DSPSplitComplex(realp: rBuf.baseAddress!, imagp: iBuf.baseAddress!)
                windowed.withUnsafeBufferPointer { wBuf in
                    wBuf.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: halfN) { cPtr in
                        vDSP_ctoz(cPtr, 2, &split, 1, vDSP_Length(halfN))
                    }
                }
                vDSP_fft_zrip(fftSetup, &split, 1, log2N, FFTDirection(FFT_FORWARD))
                iBuf[0] = 0.0 // Zero packed Nyquist component on DC bin 0
                vDSP_zvabs(&split, 1, &outMag, 1, vDSP_Length(halfN))
            }
        }
        vDSP_vsmul(outMag, 1, &scale, &outMag, 1, vDSP_Length(halfN))
    }

    channelMag(winL, &magL)
    channelMag(winR, &magR)

    // Phase-invariant stereo power aggregation: M[k] = sqrt(0.5 * (|X_L[k]|^2 + |X_R[k]|^2))
    for k in 0..<halfN {
        mag[k] = sqrt(0.5 * (magL[k] * magL[k] + magR[k] * magR[k]))
    }
}

// MARK: - Main Real-Time Engine
func runEngine() {
    setbuf(stdout, nil)
    c_hue_install_signal_handlers()

    let args = CommandLine.arguments
    let velvetMode = args.contains("--velvet") || args.contains("velvet")
    let masterGain: Float = velvetMode ? 0.62 : 0.86

    let env = loadSecrets()
    guard let ip = env["HUE_BRIDGE_IP"], !ip.isEmpty else {
        fputs("Error: HUE_BRIDGE_IP missing. Run 'hue-dsp pair' or set HUE_BRIDGE_IP in ~/.config/hue-dsp/config.env\n", stderr)
        exit(1)
    }
    guard let appKey = env["HUE_APP_KEY"], !appKey.isEmpty,
          let clientKey = env["HUE_CLIENT_KEY"], !clientKey.isEmpty else {
        fputs("Error: HUE_APP_KEY / HUE_CLIENT_KEY missing. Run 'hue-dsp pair' or configure ~/.config/hue-dsp/config.env\n", stderr)
        exit(1)
    }

    guard let pskId = fetchHuePskIdentity(ip: ip, appKey: appKey) else {
        fputs("Error: Could not resolve hue-application-id from https://\(ip)/auth/v1\n", stderr)
        exit(2)
    }
    guard let zone = resolveEntertainmentZone(ip: ip, appKey: appKey, preferredId: env["HUE_ENTERTAINMENT_ID"]) else {
        fputs("Error: No Entertainment Configuration found on Hue Bridge \(ip). Create a Music Entertainment Area in the Philips Hue app first.\n", stderr)
        exit(2)
    }
    let ecId = zone.id
    let channels = zone.channels
    let numChannels = max(1, min(20, channels.count))

    guard setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: true) else {
        fputs("Error: CLIP v2 Entertainment start rejected for \(ecId)\n", stderr)
        exit(3)
    }
    let rc = c_hue_dtls_connect(ip, 2100, pskId, clientKey)
    if rc != 0 {
        fputs("Error: DTLS 1.2 handshake to \(ip):2100 failed (rc=\(rc))\n", stderr)
        _ = setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: false)
        exit(3)
    }

    // Set up CoreAudio global stereo tap
    var tapID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)
    var aggID: AudioObjectID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?

    let tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    let tapUUID = UUID()
    tapDesc.uuid = tapUUID
    tapDesc.isPrivate = true
    tapDesc.muteBehavior = .unmuted

    guard AudioHardwareCreateProcessTap(tapDesc, &tapID) == noErr else {
        fputs("Error: AudioHardwareCreateProcessTap failed\n", stderr)
        c_hue_dtls_close()
        setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: false)
        exit(4)
    }

    let aggDict: [String: Any] = [
        kAudioAggregateDeviceNameKey as String: "HueLiveDSPTap",
        kAudioAggregateDeviceUIDKey as String: UUID().uuidString,
        kAudioAggregateDeviceIsPrivateKey as String: true,
        kAudioAggregateDeviceTapAutoStartKey as String: true,
        kAudioAggregateDeviceTapListKey as String: [
            [
                kAudioSubTapUIDKey as String: tapUUID.uuidString,
                kAudioSubTapDriftCompensationKey as String: true
            ]
        ]
    ]

    guard AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggID) == noErr else {
        fputs("Error: AudioHardwareCreateAggregateDevice failed\n", stderr)
        AudioHardwareDestroyProcessTap(tapID)
        c_hue_dtls_close()
        setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: false)
        exit(5)
    }

    var sampleRate: Float64 = 48000.0
    var srSize = UInt32(MemoryLayout<Float64>.size)
    var srAddr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyNominalSampleRate,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    AudioObjectGetPropertyData(aggID, &srAddr, 0, nil, &srSize, &sampleRate)
    if sampleRate < 8000.0 { sampleRate = 48000.0 }

    let ring = StereoRingBuffer()
    let ioStatus = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inInputData, _, _, _ in
        ring.ingestAudioBufferList(inInputData)
    }

    guard ioStatus == noErr, AudioDeviceStart(aggID, procID) == noErr else {
        fputs("Error: Could not start CoreAudio tap IOProc\n", stderr)
        if let p = procID { AudioDeviceDestroyIOProcID(aggID, p) }
        AudioHardwareDestroyAggregateDevice(aggID)
        AudioHardwareDestroyProcessTap(tapID)
        c_hue_dtls_close()
        setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: false)
        exit(6)
    }

    // DSP State Setup (N = 2048 FFT @ 48 kHz -> 23.44 Hz bin resolution)
    let fftN = 2048
    let halfN = fftN / 2
    let log2N = vDSP_Length(11)
    guard let fftSetup = vDSP_create_fftsetup(log2N, FFTRadix(kFFTRadix2)) else {
        fputs("Error: vDSP_create_fftsetup failed\n", stderr)
        AudioDeviceStop(aggID, procID)
        if let p = procID { AudioDeviceDestroyIOProcID(aggID, p) }
        AudioHardwareDestroyAggregateDevice(aggID)
        AudioHardwareDestroyProcessTap(tapID)
        c_hue_dtls_close()
        setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: false)
        exit(7)
    }

    // Lock-protected initial palette snapshot taken strictly BEFORE dispatching background watcher
    let initialPalette = velvetMode ? defaultVelvetPalette : defaultStagePalette
    let paletteState = PaletteState(initialPalette: initialPalette)
    var curPalette = paletteState.snapshot().palette

    let watcherGroup = DispatchGroup()
    watcherGroup.enter()
    DispatchQueue.global(qos: .utility).async {
        defer { watcherGroup.leave() }
        var lastTrackId = ""
        let script = """
        tell application "System Events" to set isRunning to (name of processes) contains "Spotify"
        if not isRunning then return "STOPPED"
        tell application "Spotify"
          if player state is stopped then return "STOPPED"
          return (id of current track) & "|||" & (name of current track) & "|||" & (artist of current track) & "|||" & (artwork url of current track)
        end tell
        """
        while c_hue_should_stop() == 0 {
            let out = runProcess("/usr/bin/osascript", ["-e", script], timeoutSec: 2.0, abortOnStop: true)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if c_hue_should_stop() != 0 { break }
            if out != "STOPPED" && out.contains("|||") {
                let parts = out.components(separatedBy: "|||")
                if parts.count >= 4 {
                    let tid = parts[0]
                    if tid != lastTrackId {
                        lastTrackId = tid
                        let title = parts[1]
                        let artist = parts[2]
                        let artUrl = parts[3]
                        let newPal = extractPaletteFromArtwork(urlStr: artUrl, velvetMode: velvetMode)
                        paletteState.update(palette: newPal, trackLabel: "'\(title)' — \(artist)")
                        let hexes = newPal.map { $0.hex }.joined(separator: ",")
                        print("[DSP 50Hz -> Hue] Track: '\(title)' — \(artist) | Mode=\(velvetMode ? "velvet" : "auto") | Palette=\(hexes)")
                    }
                }
            }
            for _ in 0..<50 {
                if c_hue_should_stop() != 0 { break }
                Thread.sleep(forTimeInterval: 0.05)
            }
        }
    }

    var window = [Float](repeating: 0, count: fftN)
    vDSP_hann_window(&window, vDSP_Length(fftN), Int32(vDSP_HANN_NORM))

    var winL = [Float](repeating: 0, count: fftN)
    var winR = [Float](repeating: 0, count: fftN)
    var windowed = [Float](repeating: 0, count: fftN)
    var realp = [Float](repeating: 0, count: halfN)
    var imagp = [Float](repeating: 0, count: halfN)
    var magL = [Float](repeating: 0, count: halfN)
    var magR = [Float](repeating: 0, count: halfN)
    var mag = [Float](repeating: 0, count: halfN)
    var prevLogMag = [Float](repeating: 0, count: halfN)

    let binHz = Float(sampleRate) / Float(fftN)
    let bSubEnd = max(3, Int(round(120.0 / binHz)))
    let bBassEnd = max(bSubEnd + 2, Int(round(360.0 / binHz)))
    let bMidEnd = max(bBassEnd + 4, Int(round(2800.0 / binHz)))
    let bHighEnd = min(halfN - 1, Int(round(11000.0 / binHz)))

    var subTracker = BandTracker()
    var bassTracker = BandTracker()
    var midTracker = BandTracker()
    var highTracker = BandTracker()
    var leftMidTracker = BandTracker()
    var rightMidTracker = BandTracker()

    var fluxMean: Float = 0.05
    var fluxPeak: Float = 0.25
    var kickImpulse: Float = 0.0
    var kickRefractory: Int = 0

    var snareMean: Float = 0.05
    var snarePeak: Float = 0.25
    var snareImpulse: Float = 0.0
    var drumPresence: Float = 0.50

    let histLen = 300 // 6.0s @ 50 Hz
    var onsetHist = [Float](repeating: 0, count: histLen)
    var histWrite = 0

    var estimatedBpm: Float = 115.0
    var tempoConf: Float = 0.0
    var beatPhase: Float = 0.0
    var barPhase: Float = 0.0
    var smoothCentroid: Float = 0.45

    var channelIds = channels.prefix(numChannels).map { $0.channelId }
    var rgbBuf = [UInt16](repeating: 0, count: numChannels * 3)
    var seq: UInt8 = 0
    var frameCount: Int = 0
    let fps: Float = 50.0
    let dt: Float = 1.0 / fps

    print("[DSP 50Hz -> Hue] Live CoreAudio Tap (\(Int(sampleRate)) Hz) + DTLS 1.2 Entertainment Stream (50 FPS, ec=\(ecId), ch=\(numChannels)) ACTIVE.")

    var nextTick = DispatchTime.now()

    while c_hue_should_stop() == 0 {
        nextTick = nextTick + .milliseconds(20)

        ring.readWindow(count: fftN, outL: &winL, outR: &winR)

        // Phase-proof combined stereo power RMS: sqrt(0.5 * (rmsL^2 + rmsR^2))
        var rmsL: Float = 0, rmsR: Float = 0
        vDSP_rmsqv(winL, 1, &rmsL, vDSP_Length(fftN))
        vDSP_rmsqv(winR, 1, &rmsR, vDSP_Length(fftN))
        let rmsStereo = sqrt(0.5 * (rmsL * rmsL + rmsR * rmsR))

        computeStereoSpectrum(
            fftSetup: fftSetup,
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

        // 1. Sub-band RMS energies & Power Spectral Centroid (Parseval M[k]^2 weighted)
        func bandRms(_ spec: [Float], _ startBin: Int, _ endBin: Int) -> Float {
            if endBin <= startBin { return 0 }
            var acc: Float = 0
            for k in startBin...endBin {
                acc += spec[k] * spec[k]
            }
            return sqrt(acc / Float(endBin - startBin + 1))
        }

        let rawSub = bandRms(mag, 2, bSubEnd)
        let rawBass = bandRms(mag, bSubEnd + 1, bBassEnd)
        let rawMid = bandRms(mag, bBassEnd + 1, bMidEnd) * 1.8
        let rawHigh = bandRms(mag, bMidEnd + 1, bHighEnd) * 3.4
        let rawMidL = bandRms(magL, bBassEnd + 1, bMidEnd) * 1.8
        let rawMidR = bandRms(magR, bBassEnd + 1, bMidEnd) * 1.8

        let subC = subTracker.update(rawSub)
        let bassC = bassTracker.update(rawBass)
        let midC = midTracker.update(rawMid)
        let highC = highTracker.update(rawHigh)
        let midLeftC = leftMidTracker.update(rawMidL)
        let midRightC = rightMidTracker.update(rawMidR)

        var weightedPower: Float = 0
        var totalPower: Float = 1e-9
        for k in 2...bHighEnd {
            let p = mag[k] * mag[k]
            weightedPower += Float(k) * binHz * p
            totalPower += p
        }
        let centroidHz = weightedPower / totalPower
        let normCentroid = clamp01((log2(max(110.0, centroidHz) / 160.0)) / 3.8)
        smoothCentroid += 0.10 * (normCentroid - smoothCentroid)

        // 2. Half-Wave Rectified Log-Spectral Flux Onset Detectors (Kick & Snare/Hat)
        var lowFlux: Float = 0
        var hiFlux: Float = 0
        let kickTopBin = min(halfN - 1, max(6, Int(round(145.0 / binHz))))
        let snareStartBin = max(20, Int(round(1100.0 / binHz)))
        let snareEndBin = min(halfN - 1, Int(round(7500.0 / binHz)))

        for k in 2...bHighEnd {
            let lm = log1p(45.0 * mag[k])
            let diff = lm - prevLogMag[k]
            prevLogMag[k] = lm
            if diff > 0 {
                if k <= kickTopBin {
                    lowFlux += diff
                } else if k >= snareStartBin && k <= snareEndBin {
                    hiFlux += diff
                }
            }
        }
        lowFlux /= Float(max(1, kickTopBin - 1))
        hiFlux /= Float(max(1, snareEndBin - snareStartBin + 1))

        onsetHist[histWrite] = lowFlux
        histWrite = (histWrite + 1) % histLen

        fluxMean += 0.05 * (lowFlux - fluxMean)
        fluxPeak = max(fluxMean + 0.02, max(lowFlux, fluxPeak * 0.994))
        let kickThresh = fluxMean * 1.42 + 0.015

        kickImpulse *= 0.83 // crisp ~130ms decay
        if kickRefractory > 0 { kickRefractory -= 1 }

        var kickTriggered = false
        if lowFlux > kickThresh && kickRefractory == 0 && rmsStereo > 0.004 {
            let strength = clamp01((lowFlux - fluxMean) / max(0.02, fluxPeak - fluxMean))
            if strength > 0.24 {
                kickImpulse = max(kickImpulse, min(1.0, 0.35 + 0.85 * strength))
                kickRefractory = 8 // 160ms refractory gate
                kickTriggered = true
            }
        }

        snareMean += 0.06 * (hiFlux - snareMean)
        snarePeak = max(snareMean + 0.015, max(hiFlux, snarePeak * 0.994))
        snareImpulse *= 0.78 // fast ~95ms transient decay
        if hiFlux > snareMean * 1.48 + 0.01 && rmsStereo > 0.004 {
            let sStr = clamp01((hiFlux - snareMean) / max(0.015, snarePeak - snareMean))
            snareImpulse = max(snareImpulse, min(1.0, sStr * 1.15))
        }

        // Track percussive presence so ambient vocal/pad sections float organically while drum drops lock hard
        let rawDrumDrive = clamp01(max(kickImpulse, 0.65 * subC + 0.55 * snareImpulse))
        if rawDrumDrive > drumPresence {
            drumPresence += 0.35 * (rawDrumDrive - drumPresence)
        } else {
            drumPresence = max(0.16, drumPresence * 0.988) // ~1.65s hold/release
        }

        // 3. Autocorrelation Tempo Estimator (every 0.5s = 25 frames)
        if frameCount >= 100 && (frameCount % 25 == 0) {
            var meanH: Float = 0
            vDSP_meanv(onsetHist, 1, &meanH, vDSP_Length(histLen))
            var centered = [Float](repeating: 0, count: histLen)
            var negMean = -meanH
            vDSP_vsadd(onsetHist, 1, &negMean, &centered, 1, vDSP_Length(histLen))

            var energy0: Float = 0
            vDSP_dotpr(centered, 1, centered, 1, &energy0, vDSP_Length(histLen))

            if energy0 > 1e-5 {
                var corr = [Float](repeating: 0, count: 48)
                var bestLag = 26
                var bestScore: Float = -1.0

                for lag in 18...46 {
                    var ac: Float = 0
                    for n in lag..<histLen {
                        let idxNew = (histWrite + n) % histLen
                        let idxOld = (histWrite + n - lag) % histLen
                        ac += centered[idxNew] * centered[idxOld]
                    }
                    let normAc = ac / energy0
                    corr[lag] = normAc

                    let candBpm = 60.0 * fps / Float(lag)
                    let logDiff = log2(candBpm / 108.0)
                    let prior = exp(-0.5 * (logDiff * logDiff) / (0.52 * 0.52))
                    let contDiff = abs(candBpm - estimatedBpm) / estimatedBpm
                    let contBonus: Float = contDiff < 0.08 ? 1.12 : 1.0
                    let score = normAc * prior * contBonus
                    if score > bestScore {
                        bestScore = score
                        bestLag = lag
                    }
                }

                var refinedLag = Float(bestLag)
                if bestLag > 18 && bestLag < 46 {
                    let y1 = corr[bestLag - 1]
                    let y2 = corr[bestLag]
                    let y3 = corr[bestLag + 1]
                    let denom = y1 - 2.0 * y2 + y3
                    if abs(denom) > 1e-6 {
                        let delta = 0.5 * (y1 - y3) / denom
                        if abs(delta) < 1.0 {
                            refinedLag += delta
                        }
                    }
                }

                let rawBpm = 60.0 * fps / refinedLag
                let conf = clamp01(corr[bestLag] * 2.4)
                tempoConf = 0.7 * tempoConf + 0.3 * conf
                if conf > 0.12 {
                    if abs(rawBpm - estimatedBpm) < 10.0 {
                        estimatedBpm += 0.25 * (rawBpm - estimatedBpm)
                    } else if conf > 0.30 {
                        estimatedBpm = rawBpm
                    }
                }
            }
        }

        // 4. Phase-Locked Beat & Bar Oscillators
        let beatsPerSec = estimatedBpm / 60.0
        beatPhase = mod1(beatPhase + dt * beatsPerSec)
        barPhase = (barPhase + dt * beatsPerSec).truncatingRemainder(dividingBy: 4.0)

        if kickTriggered {
            var phaseErr = -beatPhase
            if phaseErr < -0.5 { phaseErr += 1.0 }
            if phaseErr > 0.5 { phaseErr -= 1.0 }
            beatPhase = mod1(beatPhase + 0.30 * phaseErr)
            let nearestBeat = round(barPhase)
            let barErr = nearestBeat - barPhase
            barPhase = (barPhase + 0.22 * barErr + 4.0).truncatingRemainder(dividingBy: 4.0)
        }

        let snap = paletteState.snapshot()
        let tgt = snap.palette
        let trackTitle = snap.trackLabel
        for i in 0..<4 {
            curPalette[i] = RGB.lerp(curPalette[i], tgt[i], 0.03)
        }

        // 5. 3D Spatial & Tempo-Locked Channel Synthesis (1..20 channels)
        let isSilent = rmsStereo < 0.0025
        let beatPulse0 = exp(-5.6 * beatPhase) * drumPresence
        let beatPulseRadial = exp(-5.6 * mod1(beatPhase - 0.22)) * drumPresence
        let beatPulseOff = exp(-5.6 * mod1(beatPhase - 0.50)) * drumPresence
        let swayLeft = pow(cos(Float.pi * barPhase * 0.5), 2.0) * drumPresence
        let swayRight = pow(sin(Float.pi * barPhase * 0.5), 2.0) * drumPresence

        let barColorShift = Int(floor(barPhase)) % 4
        let cPrimary = curPalette[0]
        let cGroove = curPalette[1]
        let cMelodic = RGB.lerp(curPalette[2], curPalette[(2 + barColorShift) % 4], 0.35)
        let cSparkle = curPalette[3]

        let floorGlow: Float = 0.10

        // Role 0 (Center Kick & Tempo Heartbeat)
        let vKick = clamp01(floorGlow + 0.52 * kickImpulse + 0.32 * beatPulse0 + 0.18 * subC)
        let colKick = RGB.lerp(cPrimary, cGroove, clamp01(0.65 * kickImpulse + 0.25 * subC)).scaled(vKick)

        // Role 4 (Front-Low Radial Bass Wavefront)
        let vBass = clamp01(floorGlow + 0.46 * bassC + 0.36 * beatPulseRadial + 0.18 * kickImpulse)
        let colBass = RGB.lerp(cGroove, cPrimary, 0.45 + 0.45 * bassC).scaled(vBass)

        // Role 3 (Left Stereo Lead/Vocals + Odd-Beat Sway)
        let vLeft = clamp01(floorGlow + 0.52 * midLeftC + 0.32 * swayLeft * (0.45 + 0.55 * tempoConf) + 0.14 * kickImpulse)
        let colLeft = RGB.lerp(cMelodic, cGroove, 1.0 - smoothCentroid).scaled(vLeft)

        // Role 2 (Right Stereo Lead + Even-Beat Sway)
        let vRight = clamp01(floorGlow + 0.52 * midRightC + 0.32 * swayRight * (0.45 + 0.55 * tempoConf) + 0.14 * snareImpulse)
        let colRight = RGB.lerp(cGroove, cSparkle, smoothCentroid).scaled(vRight)

        // Role 1 (Rear Snare Backbeat, Hi-Hat Shimmer & Off-Beat Syncopation)
        let vSnare = clamp01(floorGlow + 0.46 * snareImpulse + 0.34 * highC + 0.26 * beatPulseOff)
        let colSnare = RGB.lerp(cMelodic, cSparkle, clamp01(0.35 + 0.65 * highC + 0.35 * snareImpulse)).scaled(vSnare)

        let breath = 0.08 + 0.04 * (0.5 + 0.5 * sin(Float(frameCount) * 0.04))
        let silentCol = cPrimary.scaled(breath)

        for idx in 0..<numChannels {
            let chColor: RGB
            if isSilent {
                chColor = silentCol
            } else {
                switch channels[idx].role {
                case .centerKick: chColor = colKick
                case .frontBass: chColor = colBass
                case .leftStage: chColor = colLeft
                case .rightStage: chColor = colRight
                case .rearSnare: chColor = colSnare
                }
            }
            let r = pow(clamp01(chColor.r * masterGain), 2.1)
            let g = pow(clamp01(chColor.g * masterGain), 2.1)
            let b = pow(clamp01(chColor.b * masterGain), 2.1)
            rgbBuf[idx * 3 + 0] = UInt16(clamping: Int(round(r * 65535.0)))
            rgbBuf[idx * 3 + 1] = UInt16(clamping: Int(round(g * 65535.0)))
            rgbBuf[idx * 3 + 2] = UInt16(clamping: Int(round(b * 65535.0)))
        }

        let sendRc = c_hue_dtls_send_rgb16_channels(ecId, seq, &channelIds, &rgbBuf, Int32(numChannels))
        seq &+= 1
        if sendRc != 0 && (frameCount % 100 == 0) {
            setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: true)
            _ = c_hue_dtls_connect(ip, 2100, pskId, clientKey)
        }

        if frameCount > 0 && (frameCount % 200 == 0) {
            print(String(
                format: "[DSP Telemetry] %@ | BPM=%.1f (conf=%.2f, drums=%.2f) | Sub=%.2f Bass=%.2f Mid=%.2f High=%.2f | Kick=%.2f Snare=%.2f | Centroid=%.2f (%.0fHz)",
                trackTitle, estimatedBpm, tempoConf, drumPresence, subC, bassC, midC, highC, kickImpulse, snareImpulse, smoothCentroid, centroidHz
            ))
        }

        frameCount += 1
        let now = DispatchTime.now()
        if nextTick > now {
            let ns = nextTick.uptimeNanoseconds - now.uptimeNanoseconds
            Thread.sleep(forTimeInterval: Double(ns) / 1_000_000_000.0)
        } else {
            nextTick = now
        }
    }

    // Graceful teardown: wait for Spotify watcher, release CoreAudio tap & stop Hue Entertainment mode
    print("[DSP 50Hz -> Hue] Shutting down cleanly...")
    c_hue_request_stop()
    _ = watcherGroup.wait(timeout: .now() + 2.5)
    vDSP_destroy_fftsetup(fftSetup)
    AudioDeviceStop(aggID, procID)
    if let p = procID { AudioDeviceDestroyIOProcID(aggID, p) }
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
    c_hue_dtls_close()
    if setEntertainmentStreaming(ip: ip, appKey: appKey, ecId: ecId, active: false) {
        print("[DSP 50Hz -> Hue] Entertainment teardown acknowledged (ec=\(ecId)).")
    } else {
        fputs("Error: CLIP v2 Entertainment stop unacknowledged for \(ecId)\n", stderr)
        exit(8)
    }
}

#if !HUE_DSP_LIBRARY_TEST
@main
enum HueDSPApp {
    static func main() {
        runEngine()
    }
}
#endif
