import AppKit
import Foundation

let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
let sizes: [(String, Int)] = [("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64), ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512), ("icon_512x512", 512), ("icon_512x512@2x", 1024)]
for (name, pixels) in sizes {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else { fatalError("Cannot create icon") }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    let transform = NSAffineTransform()
    transform.scale(by: CGFloat(pixels) / 1024)
    transform.concat()
    NSColor.clear.setFill()
    NSRect(x: 0, y: 0, width: 1024, height: 1024).fill()
    let square = NSBezierPath(roundedRect: NSRect(x: 94, y: 94, width: 836, height: 836), xRadius: 200, yRadius: 200)
    let gradient = NSGradient(starting: NSColor(calibratedRed: 0.96, green: 0.32, blue: 0.32, alpha: 1), ending: NSColor(calibratedRed: 0.90, green: 0.23, blue: 0.25, alpha: 1))!
    gradient.draw(in: square, angle: -90)
    let ghost = NSBezierPath()
    ghost.move(to: NSPoint(x: 276, y: 285))
    ghost.line(to: NSPoint(x: 276, y: 525))
    ghost.curve(to: NSPoint(x: 748, y: 525), controlPoint1: NSPoint(x: 276, y: 850), controlPoint2: NSPoint(x: 748, y: 850))
    ghost.line(to: NSPoint(x: 748, y: 285))
    ghost.curve(to: NSPoint(x: 687, y: 264), controlPoint1: NSPoint(x: 748, y: 247), controlPoint2: NSPoint(x: 712, y: 245))
    ghost.line(to: NSPoint(x: 633, y: 296))
    ghost.line(to: NSPoint(x: 570, y: 261))
    ghost.curve(to: NSPoint(x: 517, y: 261), controlPoint1: NSPoint(x: 553, y: 251), controlPoint2: NSPoint(x: 534, y: 251))
    ghost.line(to: NSPoint(x: 453, y: 296))
    ghost.line(to: NSPoint(x: 390, y: 261))
    ghost.curve(to: NSPoint(x: 337, y: 264), controlPoint1: NSPoint(x: 373, y: 251), controlPoint2: NSPoint(x: 353, y: 252))
    ghost.line(to: NSPoint(x: 303, y: 282))
    ghost.close()
    NSColor.white.setFill()
    ghost.fill()
    NSColor(calibratedRed: 0.88, green: 0.23, blue: 0.24, alpha: 1).setFill()
    NSBezierPath(ovalIn: NSRect(x: 405, y: 505, width: 47, height: 59)).fill()
    NSBezierPath(ovalIn: NSRect(x: 572, y: 505, width: 47, height: 59)).fill()
    NSGraphicsContext.restoreGraphicsState()
    guard let data = bitmap.representation(using: .png, properties: [:]) else { fatalError("Cannot encode icon") }
    try data.write(to: directory.appendingPathComponent(name + ".png"))
}
