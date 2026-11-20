# A parameterized test's canceled cases

What `swift test --disable-xctest --verbose --event-stream-output-path events.jsonl --event-stream-version 0`
(Swift 6.4.2, testing library 2403, Xcode 27.2) wrote on 2026-11-20 for a package whose tests were these, for
`queuekeeper_apple.xctest_shards.canceled_cases` (vocaro/queuekeeper#312):

```swift
@Test(arguments: [1, 2, 3, 4]) func cases(n: Int) throws {
    if n == 1 { try Test.cancel("Unmeasured: the planted wait") }
    #expect(doubled(n) == n * 2)
}

@Test(arguments: [1, 2]) func skipping(n: Int) throws {
    if n == 2 { try Test.cancel("a planted skip") }
    #expect(doubled(n) == n * 2)
}

@Test func fine() { #expect(doubled(1) == 2) }
```

- `console.log`: Swift Testing's lines, from `Test run started` on. Each canceled case, and its test, is
  printed as passed, and the run's total counts three tests.
- `events.jsonl`: the event stream, whose `testCaseCancelled` events alone record the two canceled cases,
  each with its reason in `_comments`. The package's path is replaced by `/Users/someone/Cases`.

Replace them only with output recorded the same way.
