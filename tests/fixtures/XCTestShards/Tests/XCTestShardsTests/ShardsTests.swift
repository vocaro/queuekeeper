import Foundation
import Testing
import XCTest
import XCTestShards

// Two classes that pass, one that skips, as unmeasured when XCTEST_SHARDS_UNMEASURED is set, and one
// that fails only when XCTEST_SHARDS_FAIL is set: a run with either set is a negative control. A
// Swift Testing test runs beside them.
final class DoublingTests: XCTestCase {
    func testTwo() { XCTAssertEqual(doubled(2), 4) }
    func testThree() { XCTAssertEqual(doubled(3), 6) }
}

final class ZeroTests: XCTestCase {
    func testZero() { XCTAssertEqual(doubled(0), 0) }
}

final class SkippingTests: XCTestCase {
    func testSkips() throws {
        let unmeasured = ProcessInfo.processInfo.environment["XCTEST_SHARDS_UNMEASURED"] != nil
        throw XCTSkip(unmeasured ? "Unmeasured: the fixture's planted result" : "the fixture's planted skip")
    }
}

final class FailingTests: XCTestCase {
    func testFailsOnRequest() {
        XCTAssertNil(ProcessInfo.processInfo.environment["XCTEST_SHARDS_FAIL"], "the fixture's planted failure")
    }
}

@Test func doublingInSwiftTesting() { #expect(doubled(4) == 8) }
