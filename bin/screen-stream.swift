// screen-stream.swift - stream the newest screen frame to a file as JPEG and inject input.
//
//   screen-stream <out.jpg> [max-fps] [jpeg-quality] [max-width]
//
// `screencapture` costs ~0.15 s per invocation, so a capture loop tops out near 6 fps.
// ScreenCaptureKit delivers compositor frames instead. Frames are written to <out>.tmp
// and renamed, so a reader never sees a half-written JPEG (a truncated baseline JPEG
// fails to decode in the browser). Each frame is a one-line header then a JPEG:
//
//   RECT F x y w h seq   full picture
//   RECT P x y w h seq   only the rectangle that changed since the previous frame
//
// A partial frame assumes its predecessor is on screen, so a reader that misses one
// must skip partials and ask for a full frame (`{"t":"full"}` on the input socket);
// one full frame is written every 30 frames regardless. A heartbeat <out>.beat and a
// stat file <out>.stat tell the server whether capture is alive and how fast it is.
//
// Input is injected in-process over 127.0.0.1:<INPUT_PORT> (default 7777), one JSON
// command per line. Spawning cliclick or osascript per event costs ~150 ms and lets
// concurrent requests overtake each other; posting CGEvents here takes about a
// millisecond and keeps the order the user typed.
//
// Build: xcrun swiftc -O -swift-version 5 -o screen-stream bin/screen-stream.swift
import Foundation
import Darwin
import Network
import ApplicationServices
import ScreenCaptureKit
import CoreVideo
import CoreMedia
import ImageIO
import UniformTypeIdentifiers

func err(_ line: String) {
    FileHandle.standardError.write((line + "\n").data(using: .utf8)!)
}

let arguments = CommandLine.arguments
let outPath = arguments.count > 1 ? arguments[1] : "/tmp/desktop-frame.jpg"
var maxFPS = arguments.count > 2 ? (Double(arguments[2]) ?? 60) : 60
var quality = arguments.count > 3 ? (Double(arguments[3]) ?? 0.55) : 0.55
var maxWidth = arguments.count > 4 ? (Int(arguments[4]) ?? 1280) : 1280
let inputPort = UInt16(ProcessInfo.processInfo.environment["INPUT_PORT"] ?? "7777") ?? 7777
let beatPath = outPath + ".beat"
let statPath = outPath + ".stat"

var displayWidth = 0
var displayHeight = 0

func jpegData(_ buffer: CVPixelBuffer, quality: Double,
              rect: (x: Int, y: Int, w: Int, h: Int)?) -> Data? {
    CVPixelBufferLockBaseAddress(buffer, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
    let width = CVPixelBufferGetWidth(buffer)
    let height = CVPixelBufferGetHeight(buffer)
    let stride = CVPixelBufferGetBytesPerRow(buffer)
    guard stride >= width * 4 else { return nil }
    let box = rect ?? (0, 0, width, height)
    guard box.w > 0, box.h > 0, box.x >= 0, box.y >= 0,
          box.x + box.w <= width, box.y + box.h <= height else { return nil }
    let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue
        | CGBitmapInfo.byteOrder32Little.rawValue)
    let rowBytes = box.w * 4
    let provider: CGDataProvider
    if rect == nil {
        guard let whole = CGDataProvider(dataInfo: nil, data: base, size: stride * height,
                                         releaseData: { _, _, _ in }) else { return nil }
        provider = whole
    } else {
        let bytes = UnsafeMutableRawPointer.allocate(byteCount: rowBytes * box.h,
                                                     alignment: 64)
        for row in 0..<box.h {
            memcpy(bytes.advanced(by: row * rowBytes),
                   UnsafeRawPointer(base).advanced(by: (box.y + row) * stride + box.x * 4),
                   rowBytes)
        }
        guard let crop = CGDataProvider(
            dataInfo: nil, data: bytes, size: rowBytes * box.h,
            releaseData: { _, data, _ in
                UnsafeMutableRawPointer(mutating: data).deallocate()
            }) else { return nil }
        provider = crop
    }
    guard let image = CGImage(width: box.w, height: box.h, bitsPerComponent: 8,
                              bitsPerPixel: 32, bytesPerRow: rowBytes,
                              space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: info,
                              provider: provider, decode: nil,
                              shouldInterpolate: false, intent: .defaultIntent) else { return nil }
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(
        data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(destination, image,
        [kCGImageDestinationLossyCompressionQuality: NSNumber(value: quality)] as CFDictionary)
    guard CGImageDestinationFinalize(destination) else { return nil }
    return data as Data
}

func jpegPixels(_ pixels: [UInt8], width: Int, height: Int, quality: Double) -> Data? {
    // Encode a packed 32-bit BGRA buffer, so a frame the compositor already delivered
    // can be written again without waiting for another capture.
    let rowBytes = width * 4
    guard width > 0, height > 0, pixels.count == rowBytes * height else { return nil }
    return pixels.withUnsafeBytes { raw -> Data? in
        guard let base = raw.baseAddress,
              let provider = CGDataProvider(dataInfo: nil, data: base, size: raw.count,
                                            releaseData: { _, _, _ in }) else { return nil }
        let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue
            | CGBitmapInfo.byteOrder32Little.rawValue)
        guard let image = CGImage(width: width, height: height, bitsPerComponent: 8,
                                  bitsPerPixel: 32, bytesPerRow: rowBytes,
                                  space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: info,
                                  provider: provider, decode: nil, shouldInterpolate: false,
                                  intent: .defaultIntent) else { return nil }
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(
            data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image,
            [kCGImageDestinationLossyCompressionQuality: NSNumber(value: quality)] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return data as Data
    }
}

final class Sink: NSObject, SCStreamOutput, SCStreamDelegate {
    let path: String
    let temporary: String
    let interval: Double
    let quality: Double
    var last = 0.0
    var encoded = 0
    var delivered = 0
    var skipped = 0
    var encodeMS = 0.0
    var previous: [UInt8]?
    var previousWidth = 0
    var previousHeight = 0
    var forceFull = true
    var sequence = 0
    var sinceFull = 0
    var reportAt = 0.0
    var reportEncoded = 0
    var onStop: (() -> Void)?

    init(path: String, maxFPS: Double, quality: Double) {
        self.path = path
        self.temporary = path + ".tmp"
        self.interval = maxFPS > 0 ? 1.0 / maxFPS : 0
        self.quality = quality
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid else { return }
        delivered += 1
        let now = Date().timeIntervalSince1970
        if interval > 0 && now - last < interval { skipped += 1; return }
        last = now          // arrival time: the encode itself must not lower the rate
        guard let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            skipped += 1
            return
        }
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { skipped += 1; return }
        let width = CVPixelBufferGetWidth(buffer)
        let height = CVPixelBufferGetHeight(buffer)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        let rowBytes = width * 4

        // Diff against the last frame the client was sent: an unchanged frame costs
        // nothing, and a changed one carries only the rectangle that changed. A
        // terminal keystroke is a few kilobytes instead of a full-screen JPEG.
        var changed = forceFull
        var box: (x: Int, y: Int, w: Int, h: Int)? = nil
        if !forceFull, let old = previous, previousWidth == width,
           previousHeight == height {
            var top = height, bottom = -1, left = width, right = -1
            old.withUnsafeBufferPointer { prev in
                guard let prevBase = prev.baseAddress else { return }
                for y in 0..<height {
                    let current = UnsafeRawPointer(base).advanced(by: y * stride)
                    let oldRow = UnsafeRawPointer(prevBase).advanced(by: y * rowBytes)
                    if memcmp(current, oldRow, rowBytes) == 0 { continue }
                    changed = true
                    if y < top { top = y }
                    bottom = y
                    let a = current.assumingMemoryBound(to: UInt32.self)
                    let b = oldRow.assumingMemoryBound(to: UInt32.self)
                    var x0 = 0
                    while x0 < width && a[x0] == b[x0] { x0 += 1 }
                    var x1 = width - 1
                    while x1 > x0 && a[x1] == b[x1] { x1 -= 1 }
                    if x0 < left { left = x0 }
                    if x1 > right { right = x1 }
                }
            }
            if changed && bottom >= top && right >= left {
                box = (left, top, right - left + 1, bottom - top + 1)
            }
        }
        // Remember this frame as the client's picture, whatever we decide to send.
        var copy = previous ?? []
        if copy.count != rowBytes * height {
            copy = [UInt8](repeating: 0, count: rowBytes * height)
        }
        copy.withUnsafeMutableBufferPointer { out in
            guard let outBase = out.baseAddress else { return }
            for y in 0..<height {
                memcpy(UnsafeMutableRawPointer(outBase).advanced(by: y * rowBytes),
                       UnsafeRawPointer(base).advanced(by: y * stride), rowBytes)
            }
        }
        previous = copy
        previousWidth = width
        previousHeight = height
        if !changed {
            skipped += 1
            return
        }
        forceFull = false
        // A large change is cheaper as one full frame than as a crop plus headers.
        let area = box.map { $0.w * $0.h } ?? (width * height)
        let keyframe = sinceFull >= 30          // about a second at 30 fps
        let full = keyframe || box == nil || area * 100 >= width * height * 60
        let started = DispatchTime.now().uptimeNanoseconds
        guard let jpeg = jpegData(buffer, quality: quality,
                                  rect: full ? nil : box) else { skipped += 1; return }
        let ms = Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000
        encodeMS = encodeMS == 0 ? ms : encodeMS * 0.8 + ms * 0.2
        // Every frame carries a sequence number: a crop only makes sense on top of the
        // frame before it, so a client that misses one must ask for a full frame
        // instead of painting the next crop onto a stale picture. A keyframe every
        // KEYFRAME_FRAMES heals a client even if the request is lost.
        sequence += 1
        sinceFull = full ? 0 : sinceFull + 1
        let header: String
        if full {
            header = "RECT F 0 0 \(width) \(height) \(sequence)\n"
        } else {
            header = "RECT P \(box!.x) \(box!.y) \(box!.w) \(box!.h) \(sequence)\n"
        }
        var payload = Data(header.utf8)
        payload.append(jpeg)
        write(payload)
        if encoded % 120 == 0 { err("frames \(encoded) \(payload.count) bytes \(Int(ms))ms") }
    }

    private func write(_ payload: Data) {
        do {
            try payload.write(to: URL(fileURLWithPath: temporary))
        } catch {
            err("write failed: \(error.localizedDescription)")
            return
        }
        let renamed = temporary.withCString { from in path.withCString { to in rename(from, to) } }
        if renamed != 0 { return }
        encoded += 1
    }

    /// Rewrite the last captured framebuffer as a full frame. A client asks for this
    /// when it missed a crop, and the screen may be idle: waiting for the compositor to
    /// deliver another frame could leave it frozen until the next change. The buffer is
    /// the sender's current picture, so the client lands exactly on the state the next
    /// crop will assume.
    func replayFull() {
        let width = previousWidth, height = previousHeight
        guard width > 0, height > 0, let pixels = previous,
              let jpeg = jpegPixels(pixels, width: width, height: height, quality: quality)
        else { return }
        sequence += 1
        sinceFull = 0
        var payload = Data("RECT F 0 0 \(width) \(height) \(sequence)\n".utf8)
        payload.append(jpeg)
        write(payload)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        err("stream stopped: \(error.localizedDescription)")
        onStop?()
    }
}

var sinkRef: Sink?
var streamRef: SCStream?
// One serial queue owns the framebuffer: samples and the replay a client asks for.
let sampleQueue = DispatchQueue(label: "frames")
var starting = false

func startStream() {
    if starting { return }
    starting = true
    Task {
        defer { starting = false }
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                                              onScreenWindowsOnly: true)
            guard let display = content.displays.first else {
                err("no displays")
                exit(2)
            }
            let configuration = SCStreamConfiguration()
            let scale = min(1.0, Double(maxWidth) / Double(display.width))
            configuration.width = max(2, Int(Double(display.width) * scale))
            configuration.height = max(2, Int(Double(display.height) * scale))
            configuration.minimumFrameInterval = CMTime(value: 1,
                timescale: CMTimeScale(max(1, Int(maxFPS))))
            configuration.queueDepth = 2
            configuration.pixelFormat = kCVPixelFormatType_32BGRA
            configuration.showsCursor = true
            let filter = SCContentFilter(display: display, excludingWindows: [])
            let sink = Sink(path: outPath, maxFPS: maxFPS, quality: quality)
            sink.onStop = {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { startStream() }
            }
            sinkRef = sink
            displayWidth = display.width
            displayHeight = display.height
            if let old = streamRef { try? await old.stopCapture() }
            let stream = SCStream(filter: filter, configuration: configuration, delegate: sink)
            streamRef = stream
            try stream.addStreamOutput(sink, type: .screen,
                                       sampleHandlerQueue: sampleQueue)
            try await stream.startCapture()
            err("streaming \(configuration.width)x\(configuration.height) at \(Int(maxFPS)) fps to \(outPath)")
        } catch {
            err("start failed: \(error.localizedDescription); retrying")
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { startStream() }
        }
    }
}

// MARK: - input injection

final class InputAgent {
    private let listener: NWListener
    private let port: UInt16
    private let queue = DispatchQueue(label: "input")
    private let source = CGEventSource(stateID: .hidSystemState)

    init?(port: UInt16) {
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = true
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
        guard let listener = try? NWListener(using: parameters) else { return nil }
        self.listener = listener
        self.port = port
    }

    func start() {
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready:
                err("input agent on 127.0.0.1:\(self.port)")
            case let .failed(error):
                err("input agent failed: \(error.localizedDescription)")
            default:
                break
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self else { connection.cancel(); return }
            connection.start(queue: self.queue)
            self.receive(connection, buffer: Data())
        }
        listener.start(queue: queue)
    }

    private func receive(_ connection: NWConnection, buffer: Data) {
        var buffer = buffer
        connection.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [weak self] data, _, complete, error in
            guard let self else { connection.cancel(); return }
            if let data { buffer.append(data) }
            var start = buffer.startIndex
            while let newline = buffer[start...].firstIndex(of: 0x0A) {
                self.handle(buffer[start..<newline])
                start = buffer.index(after: newline)
            }
            buffer = Data(buffer[start...])
            if complete || error != nil { connection.cancel(); return }
            self.receive(connection, buffer: buffer)
        }
    }

    private func handle(_ line: Data) {
        guard !line.isEmpty,
              let object = try? JSONSerialization.jsonObject(with: Data(line)),
              let message = object as? [String: Any],
              let kind = message["t"] as? String else { return }
        switch kind {
        case "char":
            if let text = message["s"] as? String { type(text) }
        case "key":
            if let code = message["code"] as? Int {
                press(CGKeyCode(code), flags: flags(message["flags"]))
            }
        case "mouse":
            mouse(message)
        case "scroll":
            scroll(message)
        case "full":
            DispatchQueue.main.async { sinkRef?.forceFull = true }
            sampleQueue.async { sinkRef?.replayFull() }
        case "config":
            if let width = message["width"] as? Int, width > 0 { maxWidth = width }
            if let fps = message["fps"] as? Double, fps >= 1 { maxFPS = min(120, fps) }
            if let value = message["quality"] as? Double { quality = max(0.2, min(0.95, value)) }
            DispatchQueue.main.async { startStream() }
        default:
            break
        }
    }

    private func flags(_ value: Any?) -> CGEventFlags {
        var flags: CGEventFlags = []
        for name in (value as? [String]) ?? [] {
            switch name {
            case "cmd": flags.insert(.maskCommand)
            case "ctrl": flags.insert(.maskControl)
            case "alt": flags.insert(.maskAlternate)
            case "shift": flags.insert(.maskShift)
            default: break
            }
        }
        return flags
    }

    private func press(_ code: CGKeyCode, flags: CGEventFlags) {
        let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
        down?.flags = flags
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
        up?.flags = flags
        up?.post(tap: .cghidEventTap)
    }

    // AppKit applications translate keyboard events by virtual keycode and ignore
    // CGEventKeyboardSetUnicodeString, so text is typed with the keycodes of a US
    // layout; characters without one fall back to a string event.
    private static let baseKeys: [Character: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
        "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
        "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26,
        "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
        "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45,
        "m": 46, ".": 47, "`": 50, " ": 49,
    ]
    private static let shiftedKeys: [Character: Character] = [
        "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8",
        "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";",
        "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`",
    ]

    private func type(_ text: String) {
        for character in text {
            var key = character
            var shift = false
            if let base = InputAgent.shiftedKeys[character] {
                key = base
                shift = true
            } else if character.isUppercase, let lower = character.lowercased().first {
                key = lower
                shift = true
            }
            if let code = InputAgent.baseKeys[key] {
                press(code, flags: shift ? .maskShift : [])
            } else {
                unicode(character)
            }
        }
    }

    private func unicode(_ character: Character) {
        let unit = Array(String(character).utf16)
        let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true)
        down?.keyboardSetUnicodeString(stringLength: unit.count, unicodeString: unit)
        down?.post(tap: .cghidEventTap)
        let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false)
        up?.post(tap: .cghidEventTap)
    }

    private func mouse(_ message: [String: Any]) {
        guard let kind = message["type"] as? String,
              let x = message["x"] as? Double, let y = message["y"] as? Double else { return }
        let button = message["button"] as? String ?? "left"
        let count = Int64(message["count"] as? Int ?? 1)
        let point = CGPoint(x: x, y: y)
        let type: CGEventType
        let pressed: CGMouseButton
        switch (kind, button) {
        case ("move", _): type = .mouseMoved; pressed = .left
        case ("drag", _): type = .leftMouseDragged; pressed = .left
        case ("down", "right"): type = .rightMouseDown; pressed = .right
        case ("up", "right"): type = .rightMouseUp; pressed = .right
        case ("down", "middle"): type = .otherMouseDown; pressed = .center
        case ("up", "middle"): type = .otherMouseUp; pressed = .center
        case ("down", _): type = .leftMouseDown; pressed = .left
        case ("up", _): type = .leftMouseUp; pressed = .left
        default: return
        }
        if kind == "down" {
            // Put the pointer where the click lands, like real input.
            CGEvent(mouseEventSource: source, mouseType: .mouseMoved,
                    mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
        }
        let event = CGEvent(mouseEventSource: source, mouseType: type,
                            mouseCursorPosition: point, mouseButton: pressed)
        event?.setIntegerValueField(.mouseEventClickState, value: count)
        event?.post(tap: .cghidEventTap)
    }

    private func scroll(_ message: [String: Any]) {
        let dy = message["dy"] as? Double ?? 0
        let dx = message["dx"] as? Double ?? 0
        guard dy != 0 || dx != 0 else { return }
        let event = CGEvent(scrollWheelEvent2Source: source, units: .pixel,
                            wheelCount: 2, wheel1: Int32(-dy), wheel2: Int32(-dx), wheel3: 0)
        if let x = message["x"] as? Double, let y = message["y"] as? Double {
            event?.location = CGPoint(x: x, y: y)
        }
        event?.post(tap: .cghidEventTap)
    }
}

// MARK: - heartbeat and stats

func beat() {
    try? Data().write(to: URL(fileURLWithPath: beatPath))
}

func writeStat() {
    let now = Date().timeIntervalSince1970
    var fps = 0.0
    var encoded = 0, delivered = 0, skipped = 0
    var ms = 0.0
    if let sink = sinkRef {
        encoded = sink.encoded
        delivered = sink.delivered
        skipped = sink.skipped
        ms = (sink.encodeMS * 10).rounded() / 10
        if sink.reportAt > 0, now > sink.reportAt {
            fps = Double(encoded - sink.reportEncoded) / (now - sink.reportAt)
        }
        sink.reportAt = now
        sink.reportEncoded = encoded
    }
    let stat: [String: Any] = ["fps": (fps * 10).rounded() / 10,
                               "encoded": encoded, "delivered": delivered,
                               "skipped": skipped, "ms": ms,
                               "dw": displayWidth, "dh": displayHeight]
    if let data = try? JSONSerialization.data(withJSONObject: stat) {
        try? data.write(to: URL(fileURLWithPath: statPath))
    }
}

beat()
let heartbeat = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
    beat()
    writeStat()
}
heartbeat.tolerance = 0.2

if !AXIsProcessTrusted() {
    err("warning: not trusted for Accessibility; posted input will be ignored")
}
let agent = InputAgent(port: inputPort)
agent?.start()

startStream()
RunLoop.main.run()
