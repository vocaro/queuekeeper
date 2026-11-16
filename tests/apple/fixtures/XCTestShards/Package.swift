// swift-tools-version:6.0
// The package eng xctest builds once and runs in several xctest processes, and eng simulator-tests
// runs with parallel workers: their positive control, with a skip and a failure each run must
// report as such (doc/guides/parallel-tests.md).
import PackageDescription

let package = Package(
    name: "XCTestShards",
    platforms: [.iOS(.v17), .macOS(.v14)],
    targets: [
        .target(name: "XCTestShards"),
        .testTarget(name: "XCTestShardsTests", dependencies: ["XCTestShards"]),
    ]
)
