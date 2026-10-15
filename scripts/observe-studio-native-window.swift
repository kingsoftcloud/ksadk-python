// Observe an existing application process. This helper never launches an app.
import AppKit
import CoreGraphics
import Vision
import Foundation

guard CommandLine.arguments.count == 6,
      let pid = Int32(CommandLine.arguments[1]),
      let application = NSRunningApplication(processIdentifier: pid),
      let bundle = application.bundleURL,
      bundle.resolvingSymlinksInPath().standardizedFileURL.path == URL(fileURLWithPath: CommandLine.arguments[2]).resolvingSymlinksInPath().standardizedFileURL.path
else { fatalError("Existing owned native application identity is required") }
let imagePath = CommandLine.arguments[3]
let outputPath = CommandLine.arguments[4]
let marker = CommandLine.arguments[5]
application.unhide()
application.activate(options: [.activateAllWindows])
let end = Date().addingTimeInterval(20)
var observed: [String: Any]?
repeat {
    let windows = CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
    for window in windows {
        guard let owner = window[kCGWindowOwnerPID as String] as? Int32, owner == pid,
              let number = window[kCGWindowNumber as String] as? UInt32,
              let layer = window[kCGWindowLayer as String] as? Int, layer == 0,
              let bounds = window[kCGWindowBounds as String] as? [String: Any],
              let width = bounds["Width"] as? Double, width > 500,
              let height = bounds["Height"] as? Double, height > 300 else { continue }
        let capture = Process()
        capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        capture.arguments = ["-x", "-l", String(number), imagePath]
        try capture.run()
        capture.waitUntilExit()
        guard capture.terminationStatus == 0,
              let image = NSImage(contentsOfFile: imagePath)?.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
        let recognition = VNRecognizeTextRequest()
        recognition.recognitionLevel = .accurate
        recognition.usesLanguageCorrection = false
        recognition.customWords = [marker, "Agents", "AgentKit Studio"]
        try VNImageRequestHandler(cgImage: image).perform([recognition])
        let lines = recognition.results?.compactMap { $0.topCandidates(1).first?.string } ?? []
        let compact = lines.joined().filter { !$0.isWhitespace }
        let retainedSessionVisible = compact.contains(marker)
        observed = ["existingPid": pid, "windowId": number, "appPath": bundle.path,
                    "screenshotPath": imagePath, "observedAt": ISO8601DateFormatter().string(from: Date()),
                    "retainedSessionMarkerVisible": retainedSessionVisible,
                    "agentsPageEntryVisible": lines.contains { $0.contains("Agents") },
                    "recognizedText": lines, "successorLaunchedByObserver": false]
        if retainedSessionVisible { break }
    }
    if observed?["retainedSessionMarkerVisible"] as? Bool == true { break }
    RunLoop.current.run(until: Date().addingTimeInterval(1))
} while Date() < end
let result = observed ?? ["existingPid": pid, "appPath": bundle.path,
                          "retainedSessionMarkerVisible": false, "ownedWindowFound": false,
                          "successorLaunchedByObserver": false]
try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys]).write(to: URL(fileURLWithPath: outputPath))
if result["retainedSessionMarkerVisible"] as? Bool != true { exit(1) }
