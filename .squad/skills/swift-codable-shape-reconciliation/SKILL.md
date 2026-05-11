# Swift Codable Shape Reconciliation

**Confidence:** Low (single observation: iOS Codable mismatch on `/v1/identify` response). Likely to remain a pattern.

## Problem

A Swift app making network requests to a JSON API gets `"unexpected response"` errors on 200-status responses. The `JSONDecoder` throws a `DecodingError`, but the error message is generic: "The data couldn't be read because it's missing required keys" or "Expected to decode String but found a dictionary instead."

Root causes in practice:
- **Nested shape mismatch:** Server sends `{ outer: { inner } }` but Swift model expects `{ inner }`.
- **Field name mismatch:** Server sends `score` but Swift `CodingKey` expects `confidence`.
- **Type mismatch:** Server sends `"2026-05-10"` (string) but Swift property expects `Date`.
- **Optional mismatch:** Server sends `null` but Swift property is non-optional.

## Solution

1. **In `APIClient`:** Wrap `JSONDecoder().decode()` in a `do { ... } catch DecodingError { ... }` block. Print the **full `DecodingError` description** (use Swift's default description, not a custom message that loses detail).

2. **Print the raw response body** (first 500–1000 chars if large) before parsing. This is the ground truth — JSON never lies.

3. **Example pattern:**
   ```swift
   do {
       return try decoder.decode(IdentifyResponse.self, from: data)
   } catch let error as DecodingError {
       let bodyPreview = String(data: data.prefix(500), encoding: .utf8) ?? "<non-UTF8>"
       print("[APIError] DecodingError: \(error)")
       print("[APIError] Raw body: \(bodyPreview)")
       throw APIError.decoding(details: error.description)
   }
   ```

4. **In DEBUG builds, surface the error detail on screen** instead of "unexpected response." Users/testers can screenshot the full error path.

5. **Mirror the server shape in Swift exactly.** If server sends nested `match`, create a `MatchEnvelope` struct and nest it; don't flatten in the Swift model.

## When to Apply

- Every JSON API integration: wrap the decode, print error + body.
- When a 200-status request fails to decode: first instinct is to print the full `DecodingError` and raw body before debugging further.

## See Also

- [ios-engineer D-022](../../decisions.md#d-022): The concrete fix applied to `/v1/identify` response shape and `ArtworkCandidate.score` → `confidence` mapping.
