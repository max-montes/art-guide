# Decision: iOS live-mode debug fixes

**Date:** 2026-05-10
**Owner:** ios-engineer
**Status:** Active

## What was patched and why

### 1. `IdentifyResponse` model — Codable mismatch (root cause of the crash)

The Swift model was written for a flat response shape (`status`, `top_candidate`, `alternates`
at the top level) but the server returns a nested `match` envelope:

```json
{
  "request_id": "srv_...",
  "match": { "status": "exact", "confidence": 0.91, "candidates": [...] },
  "explanation": { ... },
  "diagnostics": { ... }
}
```

`JSONDecoder` threw immediately because `status` was not found at the top level.
This landed in the `.decoding` error path → "Got an unexpected response from the server."

**Fix:** Replaced stored properties with two structs that mirror the wire shape:
- `MatchEnvelope` (status, confidence, candidates)
- `IdentifyResponse` stores `requestID`, `match: MatchEnvelope`, `explanation`

Backward-compatible computed properties (`status`, `topCandidate`, `alternates`, `disclaimer`,
`style`) preserve the view and MockData surface unchanged. The memberwise init used by MockData
was kept; `disclaimer` and `style` params are accepted but ignored (computed from `match.status`
and `explanation.hedged` at runtime).

### 2. `ArtworkCandidate.confidence` CodingKey — mapped to wrong JSON key

The server sends `score` (per `docs/api.md`) for per-candidate similarity, but the CodingKey
was `case confidence` (decoding JSON key "confidence", which doesn't exist on candidates).

**Fix:** `case confidence = "score"` — decodes the server's `score` field into the Swift
`confidence` property. Views, ConfidenceBadge, and MockData are unaffected.

### 3. App Transport Security — HTTP to localhost blocked

`Info.plist` had no `NSAppTransportSecurity` entry. iOS blocks plain HTTP by default;
`http://localhost:8000` would be silently dropped by ATS before any network activity.

**Fix:** Added `NSAllowsLocalNetworking: true` to `Info.plist`. This only relaxes ATS for
`localhost`, `*.local`, and link-local addresses — the minimal required scope for local dev.

### 4. Debug diagnostics added

- `ArtGuideApp.init()` prints the resolved `AppConfig.apiBaseURL` and placeholder-key flag
  in `#if DEBUG` builds so the Xcode console confirms which URL the app actually targets.
- `APIClient.identify` and `APIClient.artwork` print the full `DecodingError` description
  and first 500 chars of the raw response body on any future decode failure.
- `APIError.decoding.userFacingMessage` surfaces the error detail on screen in DEBUG builds,
  replacing the opaque "unexpected response" message.

## Affected files

- `apps/ios/ArtGuide/Models/IdentifyResponse.swift` — full rewrite (new MatchEnvelope)
- `apps/ios/ArtGuide/Models/ArtworkCandidate.swift` — CodingKey score→confidence
- `apps/ios/ArtGuide/Models/APIError.swift` — DEBUG decode message
- `apps/ios/ArtGuide/Info.plist` — NSAllowsLocalNetworking
- `apps/ios/ArtGuide/ArtGuideApp.swift` — launch-time URL print
- `apps/ios/ArtGuide/Networking/APIClient.swift` — decode error logging
