// screen-stream.swift - stream the newest screen frame to a file as JPEG and inject input.
//
//   screen-stream <out.jpg> [max-fps] [jpeg-quality] [max-width]
//
// `screencapture` costs ~0.15 s per invocation, so a capture loop tops out near 6 fps.
// ScreenCaptureKit delivers compositor frames instead. Frames are written to <out>.tmp
// and renamed, so a reader never sees a half-written JPEG (a truncated baseline JPEG
// fails to decode in the browser). A heartbeat file <out>.beat and a stat file
// <out>.stat tell the server whether capture is alive and how fast it is.
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

func jpegData(_ buffer: CVPixelBuffer, quality: Double) -> Data? {
    CVPixelBufferLockBaseAddress(buffer, .readOnly)
    defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
    guard let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
    let width = CVPixelBufferGetWidth(buffer)
    let height = CVPixelBufferGetHeight(buffer)
    let stride = CVPixelBufferGetBytesPerRow(buffer)
    guard stride >= width * 4 else { return nil }
    let info = CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue
        | CGBitmapInfo.byteOrder32Little.rawValue)
    guard let provider = CGDataProvider(dataInfo: nil, data: base, size: stride * height,
                                        releaseData: { _, _, _ in }) else { return nil }
    guard let image = CGImage(width: width, height: height, bitsPerComponent: 8,
                              bitsPerPixel: 32, bytesPerRow: stride,
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
        // A frame that waited in the delivery queue is stale: the viewer wants the
        // newest screen, so drop it instead of encoding a backlog.
        let stamp = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        if stamp.isValid && stamp.seconds > 0 {
            let host = CMClockGetTime(CMClockGetHostTimeClock()).seconds
            if host - stamp.seconds > 0.15 { skipped += 1; return }
        }
        guard let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else {
            skipped += 1
            return
        }
        let started = DispatchTime.now().uptimeNanoseconds
        guard let data = jpegData(buffer, quality: quality) else { skipped += 1; return }
        let ms = Double(DispatchTime.now().uptimeNanoseconds - started) / 1_000_000
        encodeMS = encodeMS == 0 ? ms : encodeMS * 0.8 + ms * 0.2
        do {
            try data.write(to: URL(fileURLWithPath: temporary))
        } catch {
            err("write failed: \(error.localizedDescription)")
            return
        }
        let renamed = temporary.withCString { from in path.withCString { to in rename(from, to) } }
        if renamed != 0 { return }
        last = now
        encoded += 1
        if encoded % 120 == 0 { err("frames \(encoded) \(data.count) bytes \(Int(ms))ms") }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        err("stream stopped: \(error.localizedDescription)")
        onStop?()
    }
}

var sinkRef: Sink?
var streamRef: SCStream?
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
                                       sampleHandlerQueue: DispatchQueue(label: "frames"))
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
