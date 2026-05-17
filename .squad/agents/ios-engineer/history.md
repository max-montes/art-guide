# iOS Engineer History (Current)

## 2026-05-16 — Local Query History + TabView (D-034, D-036, D-037)

### What changed

**Files added:**
- `apps/ios/ArtGuide/Models/HistoryEntry.swift` — `@Model` class (SwiftData)
- `apps/ios/ArtGuide/Utilities/ThumbnailGenerator.swift` — JPEG thumbnail generation in pixel space
- `apps/ios/ArtGuide/Views/HistoryView.swift` — history list, row, badge, empty state, swipe-delete
- `apps/ios/ArtGuide/Views/ResultDetailView.swift` — re-renders stored result via existing `ResultView`
- `apps/ios/ArtGuideTests/ThumbnailGeneratorTests.swift` — 7 unit tests
- `apps/ios/ArtGuideTests/HistoryEntryTests.swift` — 6 persistence smoke tests

**Files modified:**
- `apps/ios/ArtGuide/ArtGuideApp.swift` — `ModelContainer` init + `.modelContainer()` modifier
- `apps/ios/ArtGuide/Views/RootView.swift` — refactored to `TabView`; old body extracted to `CameraFlowView`; `saveToHistory()` added to `CameraFlowView`
- `apps/ios/project.yml` — deployment target bumped from 16.0 → 17.0 (D-034; SwiftData requires iOS 17+)

### Architecture decisions

**ModelContainer location:** `ArtGuideApp.init()` creates `ModelContainer(for: HistoryEntry.self)`. Fatal error on failure (schema is trivial; a crash here surfaces real data-corruption issues). `.modelContainer(modelContainer)` attached to `WindowGroup` root so every descendant view has `@Environment(\.modelContext)`.

**Tab structure:**
```
RootView (TabView)
├── CameraFlowView (NavigationStack + "ArtGuide" title) [Camera tab]
└── NavigationStack → HistoryView                       [History tab]
```

**Save flow:** After `session.client.identify()` succeeds (any status):
1. `Task { await saveToHistory(image:response:) }` — fire-and-forget
2. Thumbnail generation: `Task.detached(priority: .userInitiated) { ThumbnailGenerator.generate(from:) }.value`
3. `JSONEncoder().encode(response)` → `rawResponseJSON`
4. `modelContext.insert(HistoryEntry(...))`
5. Any failure logs + returns; never surfaces to UI

**Re-rendering:** `ResultDetailView` decodes `rawResponseJSON` with `JSONDecoder` and passes `IdentifyResponse` to `ResultView`. Same view, zero duplication.

**IdentifyResponse JSON round-trip:** `IdentifyResponse` is `Codable`; stored properties (`requestID`, `match`, `explanation`) encode/decode faithfully. Computed accessors (`status`, `topCandidate`, etc.) reconstruct correctly from the decoded stored properties. Verified in `HistoryEntryTests.test_rawResponseJSON_decodesBackToIdentifyResponse`.

### Gotchas

**SwiftData + Previews:** Use `.modelContainer(for: HistoryEntry.self, inMemory: true)` in all `#Preview` blocks that touch `HistoryView` or any view with `@Environment(\.modelContext)`. Using the default on-disk container in previews can cause crashes in the Xcode preview canvas because multiple preview processes may open the same SQLite file.

**UIGraphicsImageRenderer scale factor:** The renderer defaults to the device's screen scale (e.g., 3× on iPhone 17 Pro). This means point-space `newSize` → 3× pixel output. `ThumbnailGenerator` always sets `format.scale = 1.0` to work in pixel space, ensuring the output is exactly `maxLongEdge` pixels regardless of device scale. Tests that check decoded `UIImage.size` compare against pixel dimensions (scale=1 images: `.size` == pixel size).

**SwiftData OSAllocatedUnfairLock note:** `ModelContext` is `@MainActor`-bound in the app. `saveToHistory()` is marked `@MainActor` and only spawns a detached task for the CPU thumbnail work, hopping back to main for the insert. No secondary contexts or background ModelContexts needed for this workload.

**Deployment target bump (16.0 → 17.0):** Required for SwiftData. No iOS 16-only APIs existed in the codebase; bump was clean. Brady is on a current iPhone. See D-034.

**"Never store raw images" clarification (D-036):** The project hard rule is server-side only. Local device thumbnail caching for the user's own history view is standard iOS UX and is explicitly permitted. Bearer token is never stored in `HistoryEntry`; only the response body (which does not contain it) is persisted.

### Build + test

**Deployment target:** iOS 17.0 (bumped from 16.0)
**Build:** SUCCEEDED (iPhone 17 Pro Simulator)
**Tests:** 52/52 passed (41 pre-existing + 7 ThumbnailGeneratorTests + 6 HistoryEntryTests)

---

## 2026-05-16 — Error Screen Polish (follow-up to B.1/B.2)

### What changed

**Files:** `apps/ios/ArtGuide/Models/APIError.swift`, `apps/ios/ArtGuide/Views/RootView.swift`, `apps/ios/ArtGuideTests/APIErrorTests.swift`

Brady reported that the `.decoding` error showed a wall of raw NSError text directly under the headline in the debug build. Root causes:

1. `ErrorView` used a single generic headline ("Couldn't identify that photo") for every error — wrong for network/decode errors.
2. `userFacingMessage` embedded `#if DEBUG` raw error text directly, so it rendered as-is in the view body.

**Fixes:**

- Added `APIError.headline: String` — per-case user-facing title (see mapping table below).
- Added `APIError.debugDetail: String?` — raw technical detail (decoded error text, URLError code, TLS code, HTTP body) extracted from `userFacingMessage` into a dedicated property.
- `userFacingMessage` is now always clean in all build flavors. Removed all `#if DEBUG` blocks from it. `.decoding` now returns "The server sent a response I couldn't read."
- `ErrorView` now renders `error.headline` as the title. In `#if DEBUG` builds, a collapsed `DisclosureGroup("Details")` below the body shows `debugDetail` when non-nil. In release builds, the disclosure is absent entirely.

**Headline → case mapping (canonical, do not regress):**

| `APIError` case | Headline |
|---|---|
| `.networkUnreachable` | "No internet connection" |
| `.cannotFindHost` / `.cannotConnect` | "Can't reach the museum" |
| `.timedOut` | "The server is waking up…" |
| `.tlsFailure` | "Secure connection failed" |
| `.transport` | "Network error" |
| `.http(401/403)` | "Authentication problem" |
| `.http(5xx)` | "The museum server hit a problem" |
| `.http(other)` | "Server error" |
| `.decoding` | "Something went wrong" |
| `.rateLimited` | "Slow down" |
| `.unauthorized` | "Authentication problem" |
| `.payloadTooLarge` | "Photo too large" |
| `.imageEncodingFailed` | "Photo problem" |
| `.invalidRequest` | "Request error" |
| `.cancelled` | "Upload cancelled" |

**Tests:** 41 total pass (28 pre-existing + 13 new `headline` + `debugDetail` isolation tests). **Build:** SUCCEEDED.

---

## 2026-05-16 — B.1 Error UX, B.2 Cold-Start Messages, C.3 XcodeGen Hook

### B.1 — Typed error enum + user-facing copy

Replaced the generic `APIError.transport(String)` with fine-grained network cases:
- `.networkUnreachable` ← `URLError.notConnectedToInternet / networkConnectionLost`
- `.cannotFindHost` ← `URLError.cannotFindHost / dnsLookupFailed`
- `.cannotConnect` ← `URLError.cannotConnectToHost`
- `.timedOut` ← `URLError.timedOut` → "The server is waking up. Please try again in a moment."
- `.tlsFailure(code:)` ← TLS/certificate URLError codes
- `.transport(String, code:)` ← fallthrough; in DEBUG shows raw code + message

Added `APIError.map(_ urlErr: URLError) -> APIError` static factory so both
`identify` and `artwork` catch blocks use the same mapping (one line each now).

Key copy decisions:
- `timedOut` → actionable language tied to cold-start: "The server is waking up. Please try again in a moment."
- `http(5xx)` → "The museum server hit a problem — try again in a moment."
- `http(401/403)` and `.unauthorized` → "Authentication problem — this is a bug, please report it."
- DEBUG builds always show the technical code/body; release builds show clean user copy.

Added `ArtGuideTests/APIErrorTests.swift` — 15 unit tests covering the `URLError → APIError` mapping and `userFacingMessage` spot checks.

### B.2 — Staged loading messages during cold-start

Added `LoadingMessageThreshold` constants (3s / 10s / 25s) in `LoadingView.swift`.
`LoadingView` now accepts `statusMessage: String?` with an opacity cross-fade.

In `RootView.identify()`, a background `Task` sleeps between thresholds and posts messages to `@MainActor`. Task cancelled in `defer {}` so message clears instantly on success/failure. Copy: "Waking up the museum…" → "Almost ready — first match takes a bit longer…" → "Still working on it — feel free to keep the camera steady…". Message never says "cold-start" or "container".

### C.3 — XcodeGen pre-commit hook

Created `.githooks/pre-commit` (bash, executable): checks for staged `.swift` files under `apps/ios/`, runs `xcodegen generate`, stages `ArtGuide.xcodeproj`. If `xcodegen` is absent, prints "xcodegen not found. brew install xcodegen" and exits 1.

Created `setup-hooks.sh` at repo root: `git config core.hooksPath .githooks`.

Updated `apps/ios/README.md` Quick start section with hook install one-liner.

Updated `.squad/skills/xcodegen-app-spec/SKILL.md` — bumped confidence to `very high`, added "Pre-commit hook" section describing `.githooks/pre-commit` and `setup-hooks.sh`.

Updated `.squad/skills/ios-coldstart-tolerance/SKILL.md` — added "Staged Cold-Start Messages (B.2 pattern)" section with thresholds, Task-based timer pattern, and copy rules.

**Build:** BUILD SUCCEEDED. **Tests:** 28/28 passed (13 pre-existing + 15 new `APIErrorTests`).

---

## Cold-Start Fix — 2026-05-16 (D-028)

**Symptom:** Brady hit "Could not connect to the server" on first `/identify` from real iPhone. Root cause: container scaled to zero, SigLIP load = ~20 s, URLSession.shared default timeouts fired first.

**APIClient location:** `apps/ios/ArtGuide/Networking/APIClient.swift`

**Timeout changes:**
- `URLSessionConfiguration.timeoutIntervalForRequest = 60` (per-segment inactivity)
- `URLSessionConfiguration.timeoutIntervalForResource = 90` (total request lifetime)
- `URLRequest.timeoutInterval = 60` on the `/identify` request (belt-and-suspenders)
- `APIClient` now owns its own `URLSession` instead of using `.shared`

**Warmup approach chosen: A (onAppear)**
- `APIClientProtocol.warmup()` added with default no-op extension (MockAPIClient unchanged)
- `APIClient.warmup()` fires `GET /healthz` with 30 s timeout, `try?` swallows all errors
- Called from `CaptureView.onAppear` via `Task { await session.client.warmup() }` in `RootView`
- Warmup failure never blocks identify

**Endpoints.swift:** Added `static let healthz = "/healthz"`

**Build:** BUILD SUCCEEDED, 13/13 tests pass. Committed: `0a62f8b`

**Decision inbox:** `.squad/decisions/inbox/ios-engineer-apiclient-warmup-and-timeouts.md` (promote to D-032)

**Skill:** `.squad/skills/ios-coldstart-tolerance/SKILL.md`

---
