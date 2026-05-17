# iOS Engineer History (Current)

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

## Current Status — 2026-05-16

**App scaffold complete.** SwiftUI code ready; `.xcodeproj` creation deferred to project owner. All models and views wired per `docs/data-model.md` and `docs/api.md`. Config pointing to live prod API (via `Config.local.xcconfig`).

**What's in place:**
- SwiftUI scaffold (`apps/ios/ArtGuide/` directory structure)
- Models: `IdentifyResponse`, `ArtworkCandidate`, `Explanation`, `MatchStatus`, `APIError`
- Networking layer: `APIClient` (multipart upload, bearer auth), `Endpoints`, `RateLimitInfo`
- Per-status views (exact | likely | style_only | no_match) per D-005 thresholds
- Shared components: `ConfidenceBadge` (color thresholds), `ArtworkCard` (compact + full)
- Mock data and `MockAPIClient` for Simulator preview/dev
- Image pre-processing rules: long edge ≤ 1600 px, JPEG q=0.85, EXIF stripped, 10 MB cap (per `docs/image-pipeline.md`)
- Config system: `Config.xcconfig` + `AppConfig.swift` reading `API_BASE_URL` + `API_KEY`
- Privacy strings in `Info.plist`: camera + photo library usage

**Infrastructure wired (coordinator):**
- `apps/ios/Config.local.xcconfig` → prod API: `https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io`
- Bearer token from Key Vault configured
- Swagger (`/docs`) available for manual endpoint testing

**What's NOT done yet:**
- `.xcodeproj` creation (project owner in Xcode)
- First Simulator build + test
- Switch from `MockAPIClient` to live `APIClient`
- End-to-end test (camera → upload → identify)
- App icon, splash screen, accessibility, localization

**Known operational issue (backend):**
- First request after API scale-to-zero blocks ~10–30s (model load). Subsequent <150 ms. May need retry logic or minReplicas=1 in Phase 2.

## Cross-Agent Note — 2026-05-16 (ml-retrieval-engineer)

Prod is now fully end-to-end live (D-029). You can test the iOS app against the prod URL with real Met catalog responses. Try Sunflowers as a known-good smoke test image. Expect ~3s warm response, longer on cold start (D-028 cold-start issue noted; revisit in Phase 2).

## Next Steps

1. **Create `.xcodeproj`** — Project owner to create new SwiftUI project in Xcode, merge existing source folders
2. **First Simulator build** — Verify app builds and runs with `MockAPIClient`
3. **Switch to live API** — Wire `APIClient` to prod backend
4. **End-to-end test** — Camera + upload → `/v1/identify` endpoint
5. **Monitor cold-start** — Observe latency on first post-scale request; gather data for Phase 2 cold-start decision
6. **Polish status views** — Typography, error states, retry affordance for 429 (once real responses available)
7. **Add "About" screen** — Privacy disclosure per `docs/privacy-observability.md`

## Learnings

**XcodeGen regen is mandatory after adding Swift files (second incident — 2026-05-16):**
- Symptom: cascade of "Cannot find X in scope" in Xcode for types that exist on disk.
- Root cause: `.xcodeproj` compile-phase list is static until `xcodegen generate` runs.
- Fix: `cd apps/ios && xcodegen generate` + commit the result alongside new Swift files.
- Regression prevention chosen: Documentation (prominent callout in `apps/ios/README.md`
  + `⚠️ CRITICAL` heading in `xcodegen-app-spec` SKILL.md). Pre-commit hook deferred.
- See `.squad/decisions/inbox/ios-engineer-xcodegen-regen-protocol.md`.

**MockAPIClient async lock (2026-05-16):**
- `NSLock.lock()/unlock()` inside `async func` is a Swift 6 error (warning in 5.9/5.10).
- Pattern chosen: `OSAllocatedUnfairLock<Int>` (Option A) — async-safe `withLock` closure,
  zero call-site changes, available iOS 16+ matching our deployment target.
- Did NOT convert to `actor` because `MockAPIClient`'s mutable properties are accessed
  synchronously from previews/tests and would require `await` at every access.
- See `.squad/skills/swift-actor-vs-lock/SKILL.md` for the full decision tree.

**ArtGuideTests plist fix (2026-05-16):**
- Unit-test bundle was missing `GENERATE_INFOPLIST_FILE: YES` in project.yml, causing
  code-sign failure on `xcodebuild test`. Fixed by adding to `ArtGuideTests` target settings.

**Build status 2026-05-16:** BUILD SUCCEEDED, all 13 tests pass, zero warnings.

**Config pattern works well:**
- `Config.xcconfig` + `AppConfig.swift` provides clean env override without hardcoding URLs/keys.
- Fits the "same image for local + prod" pattern (D-011).

**Multipart upload + bearer auth:**
- `URLSession.upload(for:from:delegate:)` works well for multipart image.
- Bearer token in `URLRequest.setValue(_:forHTTPHeaderField:)` is straightforward.

**Per-status view routing:**
- Clean separation of UI logic per confidence band (exact, likely, style_only, no_match).
- Never invents missing fields; always checks for nil.
- Mock data covers all 4 bands including edge cases (e.g., likelyAmbiguous with 3 candidates, gap<0.05).

See `history-archive.md` for scaffolding and early learning iterations.
