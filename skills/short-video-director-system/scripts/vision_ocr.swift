import AppKit
import Foundation
import Vision

struct OCRResult: Codable {
    let path: String
    let text: String
    let error: String?
}

func recognize(_ path: String) -> OCRResult {
    guard let image = NSImage(contentsOfFile: path) else {
        return OCRResult(path: path, text: "", error: "image_open_failed")
    }
    var rect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        return OCRResult(path: path, text: "", error: "cgimage_conversion_failed")
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return OCRResult(path: path, text: "", error: String(describing: error))
    }
    let observations = (request.results ?? []).sorted { left, right in
        let verticalDifference = abs(left.boundingBox.midY - right.boundingBox.midY)
        if verticalDifference > 0.012 {
            return left.boundingBox.midY > right.boundingBox.midY
        }
        return left.boundingBox.minX < right.boundingBox.minX
    }
    let lines = observations.compactMap { $0.topCandidates(1).first?.string }
    return OCRResult(path: path, text: lines.joined(separator: "\n"), error: nil)
}

let results = CommandLine.arguments.dropFirst().map { recognize($0) }
let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
if let data = try? encoder.encode(results), let output = String(data: data, encoding: .utf8) {
    print(output)
} else {
    FileHandle.standardError.write(Data("json_encoding_failed\n".utf8))
    exit(1)
}
