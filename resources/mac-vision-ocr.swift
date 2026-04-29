// mac-vision-ocr — Apple Vision text recognition CLI.
//
// Build:
//   swiftc -O -parse-as-library -o mac-vision-ocr mac-vision-ocr.swift \
//          -framework Cocoa -framework Vision
//
// Usage:
//   mac-vision-ocr <image-path>
//
// Loads the image (any format NSImage handles), runs VNRecognizeTextRequest,
// and prints a single JSON line to stdout:
//
//   {
//     "block_count": <int>,
//     "blocks": [{"text": "...", "confidence": 0.95, "bbox": [x,y,w,h]}, ...]
//   }
//
// bbox values are in Vision's normalized coordinates (0–1, origin bottom-left).

import Foundation
import Cocoa
import Vision

@main
struct VisionOCR {
    static func main() {
        let args = CommandLine.arguments
        guard args.count == 2 else {
            fputs("Usage: mac-vision-ocr <image-path>\n", stderr)
            exit(2)
        }
        let url = URL(fileURLWithPath: args[1])
        guard let img = NSImage(contentsOf: url),
              let tiff = img.tiffRepresentation,
              let bmp = NSBitmapImageRep(data: tiff),
              let cgImage = bmp.cgImage else {
            fputs("Failed to load image: \(args[1])\n", stderr)
            exit(3)
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        // Simplified Chinese first dramatically improves CJK recognition
        // (YouTube subtitles, Chinese chapter overlays) at the cost of
        // dropping a few pure-English chrome labels from confidence
        // 1.00 → 0.50 — labels still recognized correctly.
        request.recognitionLanguages = ["zh-Hans", "en-US"]
        request.usesLanguageCorrection = true

        let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
        do {
            try handler.perform([request])
        } catch {
            fputs("OCR failed: \(error)\n", stderr)
            exit(4)
        }

        var blocks: [[String: Any]] = []
        for obs in (request.results ?? []) {
            guard let top = obs.topCandidates(1).first else { continue }
            let bb = obs.boundingBox
            blocks.append([
                "text": top.string,
                "confidence": Double(top.confidence),
                "bbox": [Double(bb.minX), Double(bb.minY),
                         Double(bb.width), Double(bb.height)],
            ])
        }

        let result: [String: Any] = [
            "block_count": blocks.count,
            "blocks": blocks,
        ]
        guard let json = try? JSONSerialization.data(withJSONObject: result, options: []),
              let s = String(data: json, encoding: .utf8) else {
            fputs("JSON encode failed\n", stderr)
            exit(5)
        }
        print(s)
    }
}
