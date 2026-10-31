// swift-tools-version:6.0
// The package eng xctest builds once and runs in several xctest processes: its positive control,
// with a skip and a failure each process must report as such (guides/parallel-tests.md).
import PackageDescription

let package = Package(
    name: "XCTestShards",
    platforms: [.macOS(.v14)],
    targets: [
        .target(name: "XCTestShards"),
        .testTarget(name: "XCTestShardsTests", dependencies: ["XCTestShards"]),
    ]
)
