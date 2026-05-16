---
name: swift-actor-vs-lock
purpose: Choose the right synchronization primitive when Swift 6 strict-concurrency warnings fire on NSLock / NSRecursiveLock inside async functions.
when_to_use: You see "instance method 'lock' is unavailable from asynchronous contexts; Use async-safe scoped locking instead" — a Swift 6 error in strict mode that is already a warning in Swift 5.9/5.10.
last_validated: 2026-05-16
confidence: high
---

# swift-actor-vs-lock

## The warning

```
instance method 'lock' is unavailable from asynchronous contexts;
Use async-safe scoped locking instead; this is an error in the Swift 6 language mode
```

This fires when `NSLock.lock()` / `NSLock.unlock()` (or similar primitives) are called
inside an `async` function. Even if there is no `await` between lock and unlock, the
Swift concurrency runtime considers the call a hazard and will make it a hard error
under Swift 6 strict concurrency.

## Options (ranked by preference)

### Option A — `OSAllocatedUnfairLock<State>` (recommended for classes)

Use this when you have a `final class` that already conforms to a protocol and you
don't want to change the public API (no `await` on property accesses).

```swift
import os

// Before
private let lock = NSLock()
private var cursor = 0

// After
private let cursorLock = OSAllocatedUnfairLock<Int>(initialState: 0)
```

Read + mutate atomically inside `withLock`:
```swift
// Before
lock.lock()
let response = samples[cursor % samples.count]
cursor += 1
lock.unlock()
return response

// After
let response = cursorLock.withLock { cursor -> MyType in
    let r = data[cursor % data.count]
    cursor += 1
    return r
}
```

Mutate only:
```swift
cursorLock.withLock { $0 = 0 }
```

`OSAllocatedUnfairLock` is available on iOS 16+ / macOS 13+ / Swift 5.10+.
`withLock` is `nonisolated` and safe to call from any context including `async`.

### Option B — `actor` (recommended for new types / full rewrites)

Convert the class to an `actor`. Actor isolation serialises all stored-property
access automatically; no explicit lock needed.

```swift
// Before
public final class MockAPIClient: APIClientProtocol, @unchecked Sendable {
    private let lock = NSLock()
    private var cursor = 0

// After
public actor MockAPIClient: APIClientProtocol {
    private var cursor = 0
```

Trade-off: **all stored-property accesses from outside the actor now require
`await`**. If callers already use `await` (e.g. they call `async` protocol
methods) this is usually a small change. If callers access mutable properties
synchronously (e.g. `mockClient.scenario = .exact` in tests) you must add
`await`, or expose `nonisolated` helpers.

### Option C — `@MainActor` isolation

If all accesses already happen on the main thread (e.g. for an `ObservableObject`
view model), mark the class `@MainActor`. Same actor-isolation guarantee,
zero locks needed.

### ❌ Do NOT do this

```swift
// Wrong — just suppresses the diagnostic, doesn't fix the hazard
@_silgen_name or @preconcurrency import tricks
nonisolated(unsafe) var cursor = 0   // unsafe if truly shared across threads
```

## Real example — ArtGuide MockAPIClient (2026-05-16)

`MockAPIClient` was `final class … @unchecked Sendable` with `NSLock` guarding
a cycle `cursor`. It conformed to `APIClientProtocol` (methods are `async throws`).
The warning fired on `lock.lock()/unlock()` inside `identify(_:) async throws`.

**Fix chosen: Option A** (`OSAllocatedUnfairLock<Int>`) because:
- The public API surface (`scenario`, `forcedResponse`, etc.) is accessed
  synchronously from previews and tests — converting to `actor` would require
  `await` at every call site.
- `OSAllocatedUnfairLock` is available on iOS 16+, which matches the
  deployment target.
- Zero call-site changes required.
