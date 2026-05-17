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

---

### 2026-05-11 — Ingestion pipeline now fully executable

ml-retrieval-engineer fixed SigLIP embedder `.pooler_output` crash and Met HTTP 406 errors. Met Museum corpus can now be ingested end-to-end. Backend embedder cache should warm cleanly at startup. No iOS code changes needed; update backend URL to `http://localhost:8000` when ready to test live.

### 2026-05-10 — Live-mode debug: three simultaneous fixes

**Root cause identified:** `IdentifyResponse.Codable` was written for a flat schema
(`status`, `top_candidate`, `alternates` at the top level) but the server returns a nested
`match` envelope per `docs/api.md`. `JSONDecoder` threw `keyNotFound` on the missing `status`
key at the top level → `.decoding` error → "unexpected response" screen.

**Secondary bug:** `ArtworkCandidate` used `case confidence` (decoding JSON key "confidence")
but the server sends `"score"` per candidate. Fixed to `case confidence = "score"`.

**Defensive fix:** `Info.plist` had no `NSAppTransportSecurity` entry, so iOS would block
`http://localhost:8000` at ATS before any request left the Simulator. Added
`NSAllowsLocalNetworking: true`.

**Diagnostics hardened:**
- `ArtGuideApp.init()` prints the resolved `AppConfig.apiBaseURL` on launch (`#if DEBUG`).
- `APIClient` decode catch blocks print the full `DecodingError` + first 500 chars of raw body.
- `APIError.decoding.userFacingMessage` surfaces the error detail on screen in DEBUG builds.

**Files changed:**
- `Models/IdentifyResponse.swift` — full rewrite; new `MatchEnvelope` struct; computed
  view accessors (`status`, `topCandidate`, `alternates`, `disclaimer`, `style`) preserve
  all view and MockData call sites with no changes required there.
- `Models/ArtworkCandidate.swift` — CodingKey: `case confidence = "score"`.
- `Models/APIError.swift` — DEBUG-aware `decoding` message.
- `Info.plist` — `NSAllowsLocalNetworking: true`.
- `ArtGuideApp.swift` — launch-time URL print.
- `Networking/APIClient.swift` — decode error + raw body logging.

**Skill captured:** `.squad/skills/ios-local-dev/SKILL.md` — "The three things that
silently break local dev" (ATS, Codable mismatch, xcconfig propagation).

### 2026-05-10 — `/v1/identify` end-to-end live: transformers 5.x compatibility fixed

**For iOS:** Backend endpoint is now fully operational with real Met artwork matching and confidence-aware status. Exception handling improved (PIL decode errors 400, embedding failures 500). You can now flip `MockAPIClient` → `APIClient` in `ArtGuideApp.swift` and test real end-to-end flows with local or prod backend. Status-aware guardrails applied to LLM explanations; query path fully tested.


### 2026-05-10 — API error handling now returns clean 4xx envelopes

**Cross-agent:** backend-engineer fixed validation error handling in `services/api/app/main.py`. Malformed requests (e.g., string instead of UploadFile) now return a clean 400 bad_request JSON envelope instead of a 500.

**Impact for iOS:** The app's error-handling code can now assume all HTTP 4xx responses are properly formatted API errors (never internal server failures mis-reported as 4xx). Simplifies retry logic and user messaging.

### 2026-05-10 — "View on {museum}" button + debug-prefix stripping (D-026)

**Fix 1 — MuseumSourceRow:** New `Views/Components/MuseumSourceRow.swift` component renders a tasteful footer row below the explanation block in `ExactMatchView`, `LikelyMatchView`, and `StyleOnlyView`. Label is museum-name-aware ("View on {museum}"), with a "Source: {museum}" caption. Tap opens `source_url` via `UIApplication.shared.open`. Replaced the old subtle "View source" link in `ArtworkCard.full` (removed duplication). Row is gated on `candidate.sourceURL != nil` so it's invisible for `style_only` results that carry no URL.

**Fix 2 — displayText:** `Explanation.displayText` strips any leading `[…]` debug bracket (e.g. `[LLM call skipped -- AZURE_OPENAI not configured]`) via regex before text reaches the UI. `ExplanationBlock` and `NoMatchView` now use `displayText`. Defensive: the app stays correct regardless of whether backend cleans the source (note for backend-engineer: stop emitting the bracket in JSON).

**Museum-plaque fields — deferred:** D-024 tier (a) fields (`artist_bio`, `credit_line`, `dimensions`, `dynasty`) will appear in future API responses. iOS synthesized Codable ignores unknown JSON keys by default — no decoder changes needed. Wave 2 will weave these into LLM prose; no list UI will ever be added for them.

**Tests added:**
- `ArtGuideTests/ExplanationTests.swift` — 8 unit tests for `displayText` (strip, no-strip, edge cases)
- `ArtGuideTests/ArtworkCandidateTests.swift` — 5 decoding tests (`source_url` mapping, `museum`, unknown-key tolerance)
- `project.yml` updated with `ArtGuideTests` unit-test target + scheme wiring

**Files changed:** `Explanation.swift`, `MuseumSourceRow.swift` (new), `ArtworkCard.swift`, `ExactMatchView.swift`, `LikelyMatchView.swift`, `StyleOnlyView.swift`, `ResultView.swift`, `project.yml`, test files.


## 2026-05-11 — Eval bootstrap shipped; explanation UX now gated by eval thresholds

**ml-retrieval-engineer-5 closed Phase 1** by shipping an eval harness (45 test cases, 31 unit tests, thresholds, baseline). Result: **eval is now the gate** — any future change to the app's explanation UX (D-026: museum plaque prose, always-invoke LLM) must satisfy eval thresholds to ensure confidence bands behave as designed.

**Implication for you:** Your explanation UI work (plaque prose rendering, confidence-aware hedging) should now validate against the eval harness to confirm that the backend's status enum + confidence scores are correctly displayed (e.g., `exact` always shows plain prose, `style_only` hedges with "resembles", `no_match` shows re-shoot prompt). Before shipping explanation UX changes, coordinate with ml-retrieval-engineer and verify the eval still passes.

**Key baseline metrics (100-record catalog, 2026-05-10):**
- recall@1 = 1.000 (exact matches always found)
- recall@3 = 1.000 (top-3 safe for `likely` ambiguous cases)
- status_accuracy = 1.000 (confidence bands applied correctly)
- latency_p99_ms = 1385 (fast enough for interactive app)

**D-026 decision (now locked):** Explanation UX is always prose paragraph (never bulleted list), always invokes LLM (every confidence band, including `no_match`), always grounded in retrieved fields only. This confirms the LLM is non-optional and justifies its presence in the architecture.

**Files to know:**
- `services/ml/eval/run_eval.py` — how to verify your changes don't regress eval
- `services/ml/eval/dataset.jsonl` — test cases (use for manual spot checks of edge cases)
- `services/ml/eval/baseline-2026-05-11T06-30-17Z.json` — baseline snapshot
- D-025 + D-026 in decisions.md — eval design & explanation UX decision

## 2026-05-11 — Wave 1 Museum-Plaque Enrichment (Tier a) Complete

**Status:** All tier (a) enrichment shipped. iOS result views updated (D-026 iOS UX). 13 new unit tests passing. Codable forward-proofing complete; app silently ignores 7 new API fields until Wave 2.

**What shipped:**
- New `MuseumSourceRow` component renders "View on {museum}" link + "Source: {museum}" caption; opens source_url via UIApplication.shared.open.
- `Explanation.displayText` computed property strips leading `[…]` debug bracket (defensive; backend follow-up in place).
- ExactMatchView, LikelyMatchView, StyleOnlyView integrate MuseumSourceRow.
- Codable regression test ensures unknown fields (artist_bio, credit_line, dimensions, dynasty, object_wikidata_url, date_begin, date_end) are silently ignored.
- XcodeGen wiring + ArtGuideTests target integrated into scheme.

**Next:** Wave 2 (Docent/plaque LLM prompt). Awaits Azure OpenAI deployment. Will update ExplanationBlock to render richer plaque prose (single paragraph, no field list) — TODO in place.

## 2026-05-16 — Prod API live; Config.local.xcconfig wired

**Status: LIVE.** Backend deployed to `https://art-guide-prod-api.kindglacier-84ffc0b4.westus3.azurecontainerapps.io` (revision art-guide-prod-api--0000003). Embedder: `google/siglip-base-patch16-224`.

**Config changes (coordinator):**
- `apps/ios/Config.local.xcconfig` created and wired to prod API URL + bearer token from Key Vault.
- iOS can now compile and run against live backend.
- Static bearer auth configured; `/docs` Swagger available for manual endpoint testing.

**Next for ios-engineer:**
1. Create `.xcodeproj` (project owner in Xcode).
2. Verify app builds + runs in Simulator with `MockAPIClient`.
3. Switch from `MockAPIClient` to `APIClient` wired to prod.
4. End-to-end test: snap photo → upload → identify endpoint.
5. Watch for cold-start latency on first request after scale-to-zero (~10–30s), then <150 ms subsequent.

See D-028 for prod image details.


--- Archived from 2026-05-16 main history ---


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