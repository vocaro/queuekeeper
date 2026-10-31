import Foundation
import Testing
import XCTest
import XCTestShards

// Two classes that pass, one that skips and one that fails only when XCTEST_SHARDS_FAIL is set, so
// a run with it set is the negative control. A Swift Testing test runs beside them.
final class DoublingTests: XCTestCase {
    func testTwo() { XCTAssertEqual(doubled(2), 4) }
    func testThree() { XCTAssertEqual(doubled(3), 6) }
}

final class ZeroTests: XCTestCase {
    func testZero() { XCTAssertEqual(doubled(0), 0) }
}

final class SkippingTests: XCTestCase {
    func testSkips() throws { throw XCTSkip("Unmeasured: the fixture's planted skip") }
}

final class FailingTests: XCTestCase {
    func testFailsOnRequest() {
        XCTAssertNil(ProcessInfo.processInfo.environment["XCTEST_SHARDS_FAIL"], "the fixture's planted failure")
    }
}

@Test func doublingInSwiftTesting() { #expect(doubled(4) == 8) }
