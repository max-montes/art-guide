# Skill: iOS Cold-Start Tolerance — Warmup-Before-Identify Pattern

**First used:** 2026-05-16 (D-028 cold-start fix)  
**Applies to:** Any iOS app hitting a serverless/scale-to-zero backend

---

## Problem

Azure Container Apps (and similar serverless platforms) scale to zero after idle periods. First request after scale-to-zero must wait for:
1. Container boot (~2–5 s)
2. ML model weight load (SigLIP: ~10–20 s)

Total cold-start latency: 10–30 s. iOS `URLSession` default timeouts fire before the server responds, surfacing `NSURLErrorTimedOut` as Apple's misleading "Could not connect to the server" string. User thinks the app is broken.

---

## Solution: Two-Part Fix

### Part 1 — Raise URLSession Timeouts

Never use `.shared` for backends with cold-start risk. Create a custom `URLSession`:

```swift
let config = URLSessionConfiguration.default
// D-028: cold-start can take 20+ s. Survive it.
config.timeoutIntervalForRequest = 60   // per-segment inactivity
config.timeoutIntervalForResource = 90  // total request lifetime
let session = URLSession(configuration: config)
```

Also set `timeoutInterval = 60` on the heavy `URLRequest` itself (belt-and-suspenders if a custom session is injected in tests).

### Part 2 — Fire-and-Forget /healthz Warmup

Add a `warmup()` method that hits the liveness probe endpoint (`/healthz`) opportunistically:

```swift
func warmup() async {
    var request = URLRequest(url: baseURL.appendingPathComponent("/healthz"))
    request.httpMethod = "GET"
    request.timeoutInterval = 30  // shorter — this is opportunistic
    _ = try? await session.data(for: request)  // swallow all errors
}
```

**Key constraint:** warmup failure must NEVER block the real request. Use `try?` and fire-and-forget.

Trigger warmup from the capture/camera screen's `onAppear`, not at app launch. This avoids a wasted warmup if the user never reaches the camera, and maximizes the pre-warm benefit:

```swift
// In RootView or CaptureView .onAppear:
.onAppear {
    Task { await session.client.warmup() }
}
```

### Part 3 — Protocol Design

Add `warmup()` to the client protocol with a default no-op extension, so mocks and test doubles don't need to implement it:

```swift
public protocol APIClientProtocol: AnyObject, Sendable {
    func warmup() async
    // ...
}

public extension APIClientProtocol {
    func warmup() async {}  // no-op default
}
```

---

## When to Use This Pattern

- Backend uses scale-to-zero (Azure Container Apps, AWS Lambda, Google Cloud Run, Fly.io)
- Cold-start latency > 5 s (especially ML model loading)
- User flow has a "preparation screen" before the heavy API call (camera view, form, etc.)

---

## What NOT to Do

- Don't use `.shared` URLSession when cold-start is a known risk
- Don't let warmup failure surface to the user or block identify
- Don't fire warmup at app launch if the user might not reach the heavy flow
- Don't add retry logic as a substitute for timeouts (fix the timeout first)

---

## Outcome (art-guide, 2026-05-16)

Cold-start: ~20 s → user sees camera screen → container wakes → model loads → user snaps photo → identify completes in ~3 s warm latency. Zero user-visible error.
