# iOS Engineer History

## Done so far

- Scaffolded a SwiftUI app under `apps/ios/ArtGuide/` (no `.xcodeproj` yet — to be created by the project owner in Xcode).
- Models mirroring the API contract:
  - `Models/IdentifyResponse.swift`, `Models/ArtworkCandidate.swift`, `Models/Explanation.swift`, `Models/MatchStatus.swift`, `Models/APIError.swift`.
- Networking layer using `async/await` and `URLSession`:
  - `Networking/APIClient.swift` (multipart upload, bearer auth)
  - `Networking/Endpoints.swift`
  - `Networking/RateLimitInfo.swift`
- Per-status views split per `docs/data-model.md`:
  - `Views/Status/ExactMatchView.swift`
  - `Views/Status/LikelyMatchView.swift`
  - `Views/Status/StyleOnlyView.swift`
  - `Views/Status/NoMatchView.swift`
  - `Views/ResultView.swift` is now a thin router on `MatchStatus`.
- Shared components:
  - `Views/Components/ConfidenceBadge.swift` (color thresholds: ≥0.85 green, 0.6–0.85 amber, <0.6 grey)
  - `Views/Components/ArtworkCard.swift` (compact + full variants; never invents missing fields)
- Mocks aligned with the data model:
  - `exactIrises`, `likelyAmbiguous` (3 candidates, `gap<0.05`), `styleOnlyImpressionist` ("resembles"-style copy), `noMatchScene` (re-shoot tips, no artwork named).
  - `MockAPIClient.swift` returns these on demand.
- Image upload rules per `docs/image-pipeline.md`: long edge ≤ 1600 px, JPEG q=0.85, EXIF stripped, 10 MB cap.
- Configuration via `Config/Config.xcconfig` + `Config/AppConfig.swift` reading `API_BASE_URL` and `API_KEY`.
- `Info.plist` includes `NSCameraUsageDescription` and `NSPhotoLibraryUsageDescription`.

## Notes on what exists vs. what's missing

- No `.xcodeproj` yet. The project owner is installing Xcode; once installed, they will create a new SwiftUI iOS app project at `apps/ios/`, then merge or add the existing source folders to it.
- App is wired to `MockAPIClient` for previews and dev. Real backend wiring will follow once the FastAPI service points at real retrieval.
- No app icon, splash screen, accessibility audit, or localization yet.

## Next for me

1. Wait for the project owner to create the Xcode project.
2. Verify the app builds and runs in the Simulator with `MockAPIClient`.
3. Wire `APIClient` to a local backend via `API_BASE_URL` once the backend is running locally.
4. Add a small "About" screen with the privacy disclosure described in `docs/privacy-observability.md`.
5. Polish the four status views once we have real responses (typography, error states, retry affordance for 429).

## Learnings

### 2026-05-10 — pre-Run state check for max-montes

- Confirmed on disk: **no `.xcodeproj` or `.xcworkspace` exists** anywhere under
  `apps/ios/`. The Swift sources, `Info.plist`, `Config.xcconfig`, and
  `MockAPIClient` are all present and consistent with the README. No code
  edits needed before first run — the scaffold is complete enough to launch
  in the Simulator.
- `ArtGuideApp.swift` is **already wired to `MockAPIClient()`** by default,
  so the app does not require the backend to launch. Owner can hit ⌘R and
  exercise all four `MatchStatus` screens before infra is up.
- `MockAPIClient` honors the `ART_GUIDE_MOCK_SCENARIO` env var (values:
  `exact | likely | style_only | no_match | error | cycle`) — set in the
  scheme's Run > Arguments > Environment Variables to land on a specific
  status without recompiling.
- `Info.plist` already contains both `NSCameraUsageDescription` and
  `NSPhotoLibraryUsageDescription` — no edits needed for permission prompts.
- `Config.xcconfig` ships with placeholder `API_BASE_URL` and a
  `dev-replace-me` token. `AppConfig.isUsingPlaceholderKey` flags this for
  views, but mock mode ignores both. For local backend dev the value should
  be `http:/$()/localhost:8000` (note the `$()` to escape the `//` comment
  token).
- Footgun for project creation: the existing `apps/ios/ArtGuide/` folder
  collides with what Xcode wants to create when you name the new project
  `ArtGuide` and save into `apps/ios/`. Safest path is to create the
  `.xcodeproj` somewhere neutral (Desktop) and move it into `apps/ios/` as a
  sibling of the existing `ArtGuide/` source folder, then delete Xcode's
  auto-generated source files and add the on-disk `ArtGuide/` group.

## Team Update — 2026-05-10

**From Scribe:** Backend and ML engineers completed Phase 1 foundations in parallel. Decisions merged:

- **D-014 (backend-engineer):** Local infra locked in (Postgres pgvector + asyncpg + env-driven settings).
- **D-015 (ml-retrieval-engineer):** Embedding model pick — `google/siglip-base-patch16-224`, dimension **D=768**. Backend wiring schema migrations with `vector(768)` column. iOS does not need to act on this, but be aware:
  - `/v1/identify` payload shape unchanged (per D-007).
  - Confidence thresholds per D-005 assumed normalized vectors → cosine similarity as dot product.
  - No iOS code changes needed for embedding dimension; just informational for context.

iOS readiness: scaffold complete, awaiting project owner Xcode setup.

## Working rules

- Apple frameworks only (SwiftUI, Foundation, UIKit, AVFoundation, PhotosUI). No CocoaPods or SPM external deps in v1.
- `async/await` only; no Combine.
- Conform to `docs/data-model.md` for status presentation, hedging rules, and "resembles"-style copy.
- Conform to `docs/image-pipeline.md` for upload rules.
- Surface uncertainty visibly. Never overclaim confidence.


### 2026-05-10 — XcodeGen is now the source of truth for the iOS project

- Replaced the manual "create project in Xcode UI, move .xcodeproj, re-import sources, fix Info.plist + xcconfig" dance with a one-shot generator:
  - `apps/ios/project.yml` — XcodeGen spec (project name, target, bundle id `com.maxmontes.artguide`, iOS 16, Swift 5.9, automatic signing with empty `DEVELOPMENT_TEAM`, scheme env var `ART_GUIDE_MOCK_SCENARIO=cycle` pre-declared).
  - `apps/ios/setup.sh` — runs `xcodegen generate` after a `command -v` check; prints brew-install hint if missing.
  - `apps/ios/README.md` rewritten around the new "Quick start: `brew install xcodegen && cd apps/ios && ./setup.sh && open ArtGuide.xcodeproj`" flow.
  - `.gitignore` now excludes `apps/ios/ArtGuide.xcodeproj/`, `*.xcuserdata*`, and `Config.local.xcconfig`. The .xcodeproj is a build artifact.
- Critical gotcha encoded in the spec: `GENERATE_INFOPLIST_FILE = NO` + `INFOPLIST_FILE = ArtGuide/Info.plist`. Xcode's default of auto-generating an Info.plist silently shadows the on-disk one, dropping camera/photo permission strings and the `API_BASE_URL`/`API_KEY` keys. That was the root cause of the previous walkthrough's friction.
- Could not run `xcodegen` on this machine — not installed, and per task rules I did not `brew install` without max-montes' consent. YAML and shell syntax both pass static checks. First validation will happen when max-montes runs `./setup.sh`.
- Decision queued: `.squad/decisions/inbox/ios-engineer-xcodegen.md` (Active, owner ios-engineer).
- Skill captured: `.squad/skills/xcodegen-app-spec/SKILL.md` — reusable shape for "existing Info.plist + xcconfig + scheme env var" XcodeGen specs.

## Learnings

- **XcodeGen is the v1 iOS project-generation tool.** The .xcodeproj is a build artifact derived from `apps/ios/project.yml`. Edit the spec, re-run `apps/ios/setup.sh`. Never edit the .xcodeproj by hand.
- **`GENERATE_INFOPLIST_FILE` defaults to YES and silently shadows on-disk Info.plist.** The spec forces it OFF and pins `INFOPLIST_FILE = ArtGuide/Info.plist`. Anyone touching the spec must keep both settings in place or the camera/photo permission strings and API config keys disappear from the built app.
- **xcconfig wiring lives at the target level via `configFiles:`** — same file for Debug and Release; per-developer overrides go in the gitignored `Config.local.xcconfig` next to it.
- **Scheme env vars are first-class in XcodeGen.** `ART_GUIDE_MOCK_SCENARIO` is pre-declared so testers flip statuses via Edit Scheme → Run → Environment Variables without recompiling.
- **Default-argument accessibility rule (single-target apps).** Swift requires default values in a function/initializer signature to be at least as accessible as the signature itself. A `public init` whose default args reference internal types (e.g. `AppConfig.apiBaseURL`) won't compile. In a single-target iOS app there's no module boundary to cross, so `public` adds no value and just creates these traps — keep classes/inits at the default `internal` access. Surfaced when `apps/ios/ArtGuide/Networking/APIClient.swift` was marked `public` while `AppConfig` was `internal`; fixed by stripping `public` from `APIClient` and its members (Option A). `MockAPIClient` was unaffected because `MockData` is already `public`.

---

### 2026-05-11 — Backend `/v1/identify` live, ready to flip to APIClient

Backend /v1/identify is now wired and ready; flip iOS to live mode (APIClient + Config.xcconfig http://localhost:8000) to test end-to-end.
