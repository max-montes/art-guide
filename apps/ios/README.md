# ArtGuide iOS App

Native iPhone client for capturing artwork photos and presenting recognition results.

> ⚠️ **Adding Swift files? You must regen the project.**
> `cd apps/ios && xcodegen generate` — then commit the updated `.xcodeproj`.
> Skip this and Xcode shows "Cannot find X in scope" for every type in the new file.

## Status

Swift sources, assets, `Info.plist`, and `Config.xcconfig` live under
`apps/ios/ArtGuide/`. The Xcode project itself (`ArtGuide.xcodeproj`) is
**generated on demand** from [`project.yml`](./project.yml) using
[XcodeGen](https://github.com/yonaskolb/XcodeGen) — see "Quick start"
below.

## Quick start

```bash
brew install xcodegen          # one-time, if you don't already have it
cd apps/ios
./setup.sh                     # generates ArtGuide.xcodeproj from project.yml
open ArtGuide.xcodeproj
# ⌘R to run in the Simulator (uses MockAPIClient by default)
```

**First-time setup:** run `./setup-hooks.sh` from the repo root to install pre-commit hooks (or `git config core.hooksPath .githooks`). The hook auto-regens `ArtGuide.xcodeproj` whenever Swift files are staged, preventing the "Cannot find X in scope" drift issue.

That's the whole loop. The generated `.xcodeproj` is gitignored — `project.yml`
is the source of truth. To change build settings, deployment target, scheme
env vars, etc., edit `project.yml` and re-run `./setup.sh`. **Do not edit the
generated `.xcodeproj` by hand**; your changes will be wiped on the next
regenerate.

## Layout

```
apps/ios/
└── ArtGuide/
    ├── ArtGuideApp.swift            # @main entry point
    ├── Info.plist                   # camera/photo usage strings + API config keys
    ├── Config/
    │   ├── Config.xcconfig          # API_BASE_URL + API_KEY (override per build)
    │   └── AppConfig.swift          # reads config from Info.plist
    ├── Models/                      # Codable response types
    ├── Networking/                  # APIClient (URLSession, async/await)
    ├── Preview/                     # MockAPIClient + sample data
    └── Views/
        ├── RootView.swift
        ├── CaptureView.swift
        ├── LoadingView.swift
        ├── ResultView.swift
        ├── Components/
        │   ├── ConfidenceBadge.swift
        │   └── ArtworkCard.swift
        └── Status/
            ├── ExactMatchView.swift
            ├── LikelyMatchView.swift
            ├── StyleOnlyView.swift
            └── NoMatchView.swift
```

## How `project.yml` is wired

Highlights that were painful to figure out manually and are now encoded in
the spec:

- **Bundle ID:** `com.maxmontes.artguide` (works with personal Apple ID
  signing on a Simulator).
- **Deployment target:** iOS 16.0 (uses `async/await` and modern SwiftUI
  navigation).
- **`Info.plist`:** the on-disk `ArtGuide/Info.plist` is used directly
  (`GENERATE_INFOPLIST_FILE = NO`, `INFOPLIST_FILE = ArtGuide/Info.plist`).
  This is the gotcha the manual flow kept hitting — Xcode's default of
  auto-generating an `Info.plist` silently shadows the on-disk one and the
  camera/photo permission strings + `API_BASE_URL` keys never make it into
  the built app.
- **xcconfig:** `ArtGuide/Config/Config.xcconfig` is wired for both Debug
  and Release.
- **Signing:** automatic, with `DEVELOPMENT_TEAM` left empty so Xcode
  auto-fills your personal team on first open.
- **Scheme env var:** `ART_GUIDE_MOCK_SCENARIO` is pre-declared (default
  `cycle`) so you can flip the Simulator into a specific status via
  Edit Scheme → Run → Environment Variables without recompiling.

## Configuring the API base URL and bearer token

`Config.xcconfig` defines two keys:

```
API_BASE_URL = https:/$()/api.dev.artguide.example
API_KEY      = dev-replace-me
```

> The `$()` after `https:` is required — `xcconfig` files treat `//` as a
> comment delimiter, so the empty interpolation breaks the `//` token.

These are surfaced to the app via `Info.plist`:

```
<key>API_BASE_URL</key>
<string>$(API_BASE_URL)</string>
<key>API_KEY</key>
<string>$(API_KEY)</string>
```

`AppConfig.swift` reads them at runtime. To override per developer or per
build, copy `Config.xcconfig` to `Config.local.xcconfig` (gitignored) and
`#include` it from `Config.xcconfig`, or set the values via the scheme's
**Arguments ▸ Environment Variables** during development.

## Switching between mock and real API

The app picks an `APIClientProtocol` implementation in
`ArtGuideApp.swift`. By default it uses `MockAPIClient` so the app runs in
the Simulator without a backend.

```swift
// ArtGuideApp.swift
@StateObject private var session = AppSession(client: MockAPIClient())
// swap to:
// @StateObject private var session = AppSession(client: APIClient())
```

`MockAPIClient` cycles through four canned responses (exact match, likely
match, style-only, no-match) and a forced-error case so every UI state is
reachable from the Simulator.

## Status views

`ResultView` is a thin router that switches on `MatchStatus` and hands
off to one of four status-specific views in `Views/Status/`. Each view
renders only what `docs/data-model.md` permits at that confidence level.

| Status | View | What it shows |
| --- | --- | --- |
| `exact` | `ExactMatchView` | Full `ArtworkCard` (title, artist, date, medium, museum, "View source" link) plus the museum-guide explanation. Green `ConfidenceBadge`. No hedging copy added by the UI. |
| `likely` | `LikelyMatchView` | Hedged header copy, full `ArtworkCard` for the top candidate, the LLM explanation (which itself hedges), up to 2 compact alternate cards (only when the API returned alternates), and a small disclaimer. Amber `ConfidenceBadge`. |
| `style_only` | `StyleOnlyView` | No artwork title or specific attribution. Surfaces only `period`, `culture`, and `medium` from the representative candidate plus the LLM's "resembles the work of …" framing. Grey `ConfidenceBadge`. Disclaimer shown. |
| `no_match` | `NoMatchView` | Friendly "Try another shot" message plus re-shoot tips (lighting, fill the frame, get closer, hold steady). No candidate is rendered and no `ConfidenceBadge` is shown. |

Shared building blocks live in `Views/Components/`:

- `ConfidenceBadge` — pill that takes a `Double` and maps it to one of
  three buckets matching the data-model thresholds: `≥ 0.85` green,
  `0.6–0.85` amber, `< 0.6` grey. Carries an accessible label.
- `ArtworkCard` — `full` and `compact` variants. Renders an
  `AsyncImage` from the candidate's `image_url` and only draws metadata
  fields that are present; missing optionals are silently omitted (the
  card never invents data).

## Mock scenarios

`MockData` ships four canonical fixtures, one per `MatchStatus`. The
values match `docs/data-model.md` so each fixture lands cleanly in its
corresponding confidence band:

| Mock | Status | Notes |
| --- | --- | --- |
| `MockData.exactIrises` | `exact` | Single candidate, `hedged: false`, `confidence: 0.91`. |
| `MockData.likelyAmbiguous` | `likely` | Three candidates total (top + two alternates), `hedged: true`, `confidence: ~0.78`. Explanation uses hedging vocabulary ("appears to be", "most likely"). |
| `MockData.styleOnlyImpressionist` | `style_only` | One representative candidate carrying `period` / `culture` / `medium` only — no title or artist. `hedged: true`, `confidence: ~0.55`. Explanation uses "resembles the work of …" without naming a specific artwork. |
| `MockData.noMatchScene` | `no_match` | Empty candidates, `hedged: true`. Explanation says we couldn't confidently identify the photo and offers re-shoot guidance. |

`MockAPIClient` can be driven in three ways:

1. **Default (cycle).** Successive captures cycle through every mock so
   you can step through every UI state from the Simulator.
2. **Pinned scenario.** Pass an explicit `MockScenario`:
   ```swift
   AppSession(client: MockAPIClient(scenario: .styleOnly))
   ```
   Cases: `.exact`, `.likely`, `.styleOnly`, `.noMatch`, `.error` (forces
   a transport failure), and `.cycle` (the default).
3. **Environment variable.** Add `ART_GUIDE_MOCK_SCENARIO` to the
   scheme's Run > Arguments > Environment Variables. Accepted values
   match the wire enum: `exact`, `likely`, `style_only`, `no_match`,
   `error`, `cycle`. `MockAPIClient.MockScenario.fromEnvironment` reads
   it at init time, so a tester can launch straight into a particular
   screen without recompiling.

`forcedResponse` and `forcedError` are still available for ad-hoc
overrides at runtime (useful if you wire up a future dev menu).

## What's intentionally out of scope

- No on-device ML.
- No CocoaPods/SwiftPM dependencies — Apple frameworks only.
- No Combine — networking uses `async/await`.
- The app does **not** send `tone` or `length`; those are fixed server-side
  to `museum_guide` / `short`.
