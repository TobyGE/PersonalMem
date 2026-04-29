// === File: FrontCap.swift ===
// Build (fixed):
//   swiftc -O -parse-as-library -o frontcap FrontCap.swift \
//     -framework ScreenCaptureKit -framework Cocoa -framework CoreGraphics -framework CoreImage -framework ImageIO -framework UniformTypeIdentifiers
// Requires: macOS 12.3+ (ScreenCaptureKit) and Screen Recording permission for your terminal/app.

import Foundation
import Cocoa
import ScreenCaptureKit
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import CoreMedia

@main
struct FrontCapCLI {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count >= 2 else {
            fputs("Usage: frontcap <output-directory> [maxSize]\n", stderr)
            exit(2)
        }
        let outDir = URL(fileURLWithPath: args[1], isDirectory: true)
        let maxSize: CGFloat = (args.count >= 3 ? CGFloat(Int(args[2]) ?? 1024) : 1024)

        do {
            guard let targetWindowID = try getFrontmostWindowID() else {
                throw NSError(domain: "frontcap", code: 1, userInfo: [NSLocalizedDescriptionKey: "No frontmost window found"]) }

            let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: true)
            guard let scWindow = content.windows.first(where: { $0.windowID == targetWindowID }) ??
                    content.windows.max(by: { $0.frame.size.area < $1.frame.size.area }) else {
                throw NSError(domain: "frontcap", code: 2, userInfo: [NSLocalizedDescriptionKey: "Window not shareable via ScreenCaptureKit"]) }

            let filter = SCContentFilter(desktopIndependentWindow: scWindow)

            let conf = SCStreamConfiguration()
            // capturesAudio defaults to false; the explicit setter is
            // macOS 13.0+ only, so we omit it to keep the deployment
            // target at 12.3 (the SCK minimum).
            conf.minimumFrameInterval = CMTime(value: 1, timescale: 60)
            conf.queueDepth = 1
            conf.pixelFormat = kCVPixelFormatType_32BGRA

            let frameGrabber = SingleFrameGrabber(maxSize: maxSize)
            let stream = SCStream(filter: filter, configuration: conf, delegate: frameGrabber)
            try await stream.addStreamOutput(frameGrabber, type: .screen, sampleHandlerQueue: frameGrabber.queue)
            try await stream.startCapture()

            let ok = frameGrabber.waitForFrame(timeout: 3.0)
            try await stream.stopCapture()

            guard ok, let cgImage = frameGrabber.lastImage else {
                throw NSError(domain: "frontcap", code: 3, userInfo: [NSLocalizedDescriptionKey: "Failed to capture frame"]) }

            let ts = ISO8601DateFormatter()
            ts.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            let fname = ts.string(from: Date()).replacingOccurrences(of: ":", with: "-").replacingOccurrences(of: ".", with: "-") + ".png"
            let outURL = outDir.appendingPathComponent(fname)
            try FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
            try savePNG(cgImage: cgImage, to: outURL)

            print(outURL.path)
        } catch {
            fputs("frontcap error: \((error.localizedDescription))\n", stderr)
            exit(1)
        }
    }

    static func getFrontmostWindowID() throws -> CGWindowID? {
        guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
        let pid = app.processIdentifier
        let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let infoList = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else { return nil }

        var best: (id: CGWindowID, area: CGFloat)? = nil
        for info in infoList {
            guard let ownerPID = info[kCGWindowOwnerPID as String] as? pid_t, ownerPID == pid else { continue }
            guard (info[kCGWindowLayer as String] as? Int) == 0 else { continue }
            let alpha = (info[kCGWindowAlpha as String] as? CGFloat) ?? 1.0
            guard alpha > 0 else { continue }
            guard let boundsDict = info[kCGWindowBounds as String] as? [String: CGFloat],
                  let w = boundsDict["Width"], let h = boundsDict["Height"], w>0, h>0 else { continue }
            let winIDNum = info[kCGWindowNumber as String] as? NSNumber
            let winID = CGWindowID(truncating: winIDNum ?? 0)
            let area = w * h
            if best == nil || area > (best!.area) { best = (winID, area) }
        }
        return best?.id
    }
}

final class SingleFrameGrabber: NSObject, SCStreamOutput, SCStreamDelegate {
    let queue = DispatchQueue(label: "frontcap.frame.queue")
    private let ciContext = CIContext()
    private let sema = DispatchSemaphore(value: 0)
    private let maxSize: CGFloat
    private(set) var lastImage: CGImage?

    init(maxSize: CGFloat) { self.maxSize = maxSize }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of outputType: SCStreamOutputType) {
        guard outputType == .screen,
              let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ciImage = CIImage(cvImageBuffer: pixelBuffer)
        guard let cgImage = ciContext.createCGImage(ciImage, from: ciImage.extent) else { return }
        self.lastImage = resizeToFit(cgImage: cgImage, maxSide: maxSize)
        sema.signal()
    }

    func waitForFrame(timeout: TimeInterval) -> Bool {
        let t = DispatchTime.now() + timeout
        return sema.wait(timeout: t) == .success
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) { sema.signal() }

    private func resizeToFit(cgImage: CGImage, maxSide: CGFloat) -> CGImage {
        let w = CGFloat(cgImage.width), h = CGFloat(cgImage.height)
        let scale = min(1.0, maxSide / max(w, h))
        if scale >= 0.999 { return cgImage }
        let newW = Int((w * scale).rounded()), newH = Int((h * scale).rounded())
        let colorSpace = cgImage.colorSpace ?? CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(data: nil, width: newW, height: newH, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: colorSpace, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return cgImage }
        ctx.interpolationQuality = .high
        ctx.draw(cgImage, in: CGRect(x: 0, y: 0, width: newW, height: newH))
        return ctx.makeImage() ?? cgImage
    }
}

func savePNG(cgImage: CGImage, to url: URL) throws {
    let uti = UTType.png.identifier as CFString
    guard let dst = CGImageDestinationCreateWithURL(url as CFURL, uti, 1, nil) else {
        throw NSError(domain: "frontcap", code: 4, userInfo: [NSLocalizedDescriptionKey: "Cannot create image destination"]) }
    CGImageDestinationAddImage(dst, cgImage, nil)
    if !CGImageDestinationFinalize(dst) {
        throw NSError(domain: "frontcap", code: 5, userInfo: [NSLocalizedDescriptionKey: "Failed to write PNG"]) }
}

fileprivate extension CGSize { var area: CGFloat { return width * height } }

