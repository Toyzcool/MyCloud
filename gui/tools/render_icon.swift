// render_icon.swift — a standalone, zero-dependency icon renderer for MyCloud.
//
// Compiled ad-hoc by gui/build_app.sh with `swiftc` into a temp binary and run
// once to emit a full AppIcon.iconset (build_app.sh then runs iconutil to pack
// the .icns). No external assets, no packages — pure CoreGraphics + ImageIO.
//
//   render_icon <output.iconset dir>
//
// The design (drawn on a normalized 1024 grid, scaled per size):
//   • macOS squircle canvas (continuous-corner superellipse, ~100px margin)
//   • vertical gradient deep blue #1E6FE8 → #12408F
//   • a white cloud silhouette (three overlapping circles + a rounded base bar)
//     with a single subtle drop shadow
//   • an open circular sync-arrow pair (two 120° arcs + arrowheads, round caps),
//     white with a lighter-blue #9FC4FF accent on one arc, tucked at the cloud's
//     lower right.

import Foundation
import CoreGraphics
import ImageIO

// MARK: - color helpers

let deviceRGB = CGColorSpaceCreateDeviceRGB()

func color(_ r: Int, _ g: Int, _ b: Int, _ a: Double = 1) -> CGColor {
    let comps: [CGFloat] = [CGFloat(r) / 255, CGFloat(g) / 255, CGFloat(b) / 255, CGFloat(a)]
    return CGColor(colorSpace: deviceRGB, components: comps)!
}

// MARK: - geometry

/// A continuous-corner squircle (superellipse, exponent n) centered at (cx,cy)
/// with half-extent `a`. n≈5 matches Apple's rounded-rect corner feel and gives
/// an effective corner radius of ~0.28·side (≈230px on the 824px canvas box).
func squirclePath(cx: Double, cy: Double, a: Double, n: Double) -> CGPath {
    let p = CGMutablePath()
    let steps = 720
    for i in 0...steps {
        let t = Double(i) / Double(steps) * 2 * Double.pi
        let ct = cos(t), st = sin(t)
        let x = cx + a * copysign(pow(abs(ct), 2 / n), ct)
        let y = cy + a * copysign(pow(abs(st), 2 / n), st)
        let pt = CGPoint(x: x, y: y)
        if i == 0 { p.move(to: pt) } else { p.addLine(to: pt) }
    }
    p.closeSubpath()
    return p
}

/// The cloud silhouette as one path (base bar + three bumps). Filling a single
/// path with overlapping subpaths yields ONE union outline — so the drop shadow
/// is cast by the whole cloud, not by each internal edge.
func cloudPath() -> CGPath {
    let p = CGMutablePath()
    p.addRoundedRect(in: CGRect(x: 330, y: 470, width: 370, height: 120),
                     cornerWidth: 60, cornerHeight: 60)
    func bump(_ x: Double, _ y: Double, _ r: Double) {
        p.addEllipse(in: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r))
    }
    bump(415, 585, 88)    // left
    bump(515, 620, 118)   // center (largest)
    bump(620, 585, 95)    // right
    return p
}

/// Stroke one 120° arc (round caps) and cap its leading end with a filled,
/// tangential arrowhead — together the two arcs read as a circular refresh pair.
func drawSyncArc(_ ctx: CGContext, center: CGPoint, radius: Double, stroke: Double,
                 startDeg: Double, endDeg: Double, color: CGColor) {
    ctx.saveGState()
    ctx.setStrokeColor(color)
    ctx.setLineWidth(CGFloat(stroke))
    ctx.setLineCap(.round)
    let s = startDeg * .pi / 180, e = endDeg * .pi / 180
    ctx.addArc(center: center, radius: CGFloat(radius),
               startAngle: CGFloat(s), endAngle: CGFloat(e), clockwise: false)
    ctx.strokePath()

    // Arrowhead at the (counter-clockwise) leading end.
    let cx = Double(center.x), cy = Double(center.y)
    let px = cx + radius * cos(e), py = cy + radius * sin(e)     // point on circle
    let tx = -sin(e), ty = cos(e)                                // ccw tangent (travel)
    let nx = cos(e), ny = sin(e)                                 // radial (⊥ travel)
    let len = stroke * 1.7, half = stroke * 1.35
    let tip = CGPoint(x: px + tx * len, y: py + ty * len)
    let bl = CGPoint(x: px + nx * half, y: py + ny * half)
    let br = CGPoint(x: px - nx * half, y: py - ny * half)
    ctx.setFillColor(color)
    ctx.beginPath()
    ctx.move(to: tip)
    ctx.addLine(to: bl)
    ctx.addLine(to: br)
    ctx.closePath()
    ctx.fillPath()
    ctx.restoreGState()
}

// MARK: - render one size

func render(size: Int, to url: URL) -> Bool {
    guard let ctx = CGContext(data: nil, width: size, height: size,
                              bitsPerComponent: 8, bytesPerRow: 0, space: deviceRGB,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return false }

    ctx.interpolationQuality = .high
    ctx.setShouldAntialias(true)
    ctx.scaleBy(x: CGFloat(size) / 1024, y: CGFloat(size) / 1024)

    // Squircle-clipped vertical gradient (#1E6FE8 top → #12408F bottom).
    let squircle = squirclePath(cx: 512, cy: 512, a: 412, n: 5)
    ctx.saveGState()
    ctx.addPath(squircle)
    ctx.clip()
    let grad = CGGradient(colorsSpace: deviceRGB,
                          colors: [color(30, 111, 232), color(18, 64, 143)] as CFArray,
                          locations: [0, 1])!
    ctx.drawLinearGradient(grad, start: CGPoint(x: 512, y: 924),
                           end: CGPoint(x: 512, y: 100), options: [])
    ctx.restoreGState()

    // White cloud with a single subtle drop shadow.
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 26,
                  color: color(0, 0, 0, 0.20))
    ctx.addPath(cloudPath())
    ctx.setFillColor(color(255, 255, 255))
    ctx.fillPath()
    ctx.restoreGState()

    // Sync-arrow pair tucked at the cloud's lower right (over the blue field).
    let sc = CGPoint(x: 632, y: 356)
    drawSyncArc(ctx, center: sc, radius: 106, stroke: 38,
                startDeg: 235, endDeg: 355, color: color(255, 255, 255))  // white
    drawSyncArc(ctx, center: sc, radius: 106, stroke: 38,
                startDeg: 55, endDeg: 175, color: color(159, 196, 255))    // accent

    guard let image = ctx.makeImage(),
          let dest = CGImageDestinationCreateWithURL(url as CFURL, "public.png" as CFString, 1, nil)
    else { return false }
    CGImageDestinationAddImage(dest, image, nil)
    return CGImageDestinationFinalize(dest)
}

// MARK: - iconset emission

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 2 else { fail("usage: render_icon <output.iconset dir>") }
let outDir = URL(fileURLWithPath: args[1], isDirectory: true)
try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)

// Unique pixel size → the iconset filename(s) that use it (Apple convention).
let plan: [(Int, [String])] = [
    (16,   ["icon_16x16.png"]),
    (32,   ["icon_16x16@2x.png", "icon_32x32.png"]),
    (64,   ["icon_32x32@2x.png"]),
    (128,  ["icon_128x128.png"]),
    (256,  ["icon_128x128@2x.png", "icon_256x256.png"]),
    (512,  ["icon_256x256@2x.png", "icon_512x512.png"]),
    (1024, ["icon_512x512@2x.png"]),
]

for (px, names) in plan {
    let first = outDir.appendingPathComponent(names[0])
    if !render(size: px, to: first) { fail("failed to render \(px)px icon") }
    for extra in names.dropFirst() {
        let dst = outDir.appendingPathComponent(extra)
        try? FileManager.default.removeItem(at: dst)
        do { try FileManager.default.copyItem(at: first, to: dst) }
        catch { fail("failed to write \(extra): \(error.localizedDescription)") }
    }
}

print("rendered \(plan.count) sizes → \(outDir.path)")
