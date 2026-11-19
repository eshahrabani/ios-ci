// screen-stream.swift - write the newest screen frame to a file as JPEG.
//
//   screen-stream <out.jpg> [max-fps] [jpeg-quality] [max-width]
//
// `screencapture` costs ~0.15 s per invocation, so a capture loop tops out near 6 fps.
// ScreenCaptureKit delivers compositor frames instead, which is fast enough for a live
// desktop. Frames are written to <out>.tmp and renamed, so a reader never sees a
// half-written JPEG (a truncated baseline JPEG fails to decode in the browser).
//
// Build: xcrun swiftc -O -swift-version 5 -o screen-stream bin/screen-stream.swift
import Foundation
import Darwin
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
let maxFPS = arguments.count > 2 ? (Double(arguments[2]) ?? 30) : 30
let quality = arguments.count > 3 ? (Double(arguments[3]) ?? 0.6) : 0.6
let maxWidth = arguments.count > 4 ? (Int(arguments[4]) ?? 1280) : 1280

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
    var count = 0

    init(path: String, maxFPS: Double, quality: Double) {
        self.path = path
        self.temporary = path + ".tmp"
        self.interval = maxFPS > 0 ? 1.0 / maxFPS : 0
        self.quality = quality
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, sampleBuffer.isValid else { return }
        let now = Date().timeIntervalSince1970
        if interval > 0 && now - last < interval { return }
        guard let buffer = CMSampleBufferGetImageBuffer(sampleBuffer),
              let data = jpegData(buffer, quality: quality) else { return }
        do {
            try data.write(to: URL(fileURLWithPath: temporary))
        } catch {
            err("write failed: \(error.localizedDescription)")
            return
        }
        let renamed = temporary.withCString { from in path.withCString { to in rename(from, to) } }
        if renamed != 0 { return }
        last = now
        count += 1
        if count % 30 == 0 { err("frames \(count) \(data.count) bytes") }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        err("stream stopped: \(error)")
        exit(1)
    }
}

var sinkRef: Sink?
var streamRef: SCStream?

Task {
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
        configuration.queueDepth = 3
        configuration.pixelFormat = kCVPixelFormatType_32BGRA
        configuration.showsCursor = true
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let sink = Sink(path: outPath, maxFPS: maxFPS, quality: quality)
        sinkRef = sink
        let stream = SCStream(filter: filter, configuration: configuration, delegate: sink)
        streamRef = stream
        try stream.addStreamOutput(sink, type: .screen,
                                   sampleHandlerQueue: DispatchQueue(label: "frames"))
        try await stream.startCapture()
        err("streaming \(configuration.width)x\(configuration.height) at \(Int(maxFPS)) fps to \(outPath)")
    } catch {
        err("fatal: \(error)")
        exit(1)
    }
}

RunLoop.main.run()
