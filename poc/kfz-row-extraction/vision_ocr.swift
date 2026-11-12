import Foundation
import Vision
import ImageIO

struct OCRCandidate: Codable {
    let text: String
    let confidence: Float
}

struct OCRObservation: Codable {
    let text: String
    let confidence: Float
    let bboxNormalized: [Double]
    let bboxPixels: [Int]
    let alternatives: [OCRCandidate]
}

struct OCRResult: Codable {
    let image: String
    let width: Int
    let height: Int
    let recognitionLanguages: [String]
    let observations: [OCRObservation]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

guard CommandLine.arguments.count >= 2 else {
    fail("Usage: vision_ocr <image> [output.json]")
}

let imagePath = CommandLine.arguments[1]
let imageURL = URL(fileURLWithPath: imagePath)

guard let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
      let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
      let width = properties[kCGImagePropertyPixelWidth] as? Int,
      let height = properties[kCGImagePropertyPixelHeight] as? Int else {
    fail("Cannot read image dimensions: \(imagePath)")
}

let requestedLanguages = ["ru-RU", "en-US"]
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = requestedLanguages
request.usesLanguageCorrection = false
request.minimumTextHeight = 0.004

do {
    try VNImageRequestHandler(url: imageURL, options: [:]).perform([request])
} catch {
    fail("Vision OCR failed: \(error)")
}

let rawObservations = request.results ?? []
let observations: [OCRObservation] = rawObservations.compactMap { observation in
    let candidates = observation.topCandidates(3)
    guard let best = candidates.first else { return nil }

    let box = observation.boundingBox
    let left = Int((box.minX * Double(width)).rounded())
    let top = Int(((1.0 - box.maxY) * Double(height)).rounded())
    let boxWidth = Int((box.width * Double(width)).rounded())
    let boxHeight = Int((box.height * Double(height)).rounded())

    return OCRObservation(
        text: best.string,
        confidence: best.confidence,
        bboxNormalized: [box.minX, box.minY, box.width, box.height],
        bboxPixels: [left, top, boxWidth, boxHeight],
        alternatives: candidates.map { OCRCandidate(text: $0.string, confidence: $0.confidence) }
    )
}

let result = OCRResult(
    image: imagePath,
    width: width,
    height: height,
    recognitionLanguages: requestedLanguages,
    observations: observations
)

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]

do {
    let data = try encoder.encode(result)
    if CommandLine.arguments.count >= 3 {
        try data.write(to: URL(fileURLWithPath: CommandLine.arguments[2]), options: .atomic)
    } else {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
} catch {
    fail("Cannot encode OCR result: \(error)")
}
