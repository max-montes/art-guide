# Skill: iOS Local Dev Setup Checklist

**Owner:** ios-engineer  
**Applies to:** any iOS app that talks to a local backend over HTTP

## The three things that silently break local dev

### 1. App Transport Security (ATS) blocks HTTP

iOS blocks all plain HTTP by default. `http://localhost:8000` returns a transport error
before the request ever leaves the Simulator. The error appears as a `URLError` with
`NSURLErrorDomain` (code -1004 "Could not connect to the server") — it does NOT cause a
decode error or show a useful message unless you log `URLError.localizedDescription`.

**Fix in `Info.plist`:**
```xml
<key>NSAppTransportSecurity</key>
<dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
</dict>
```

Use `NSAllowsLocalNetworking` (not `NSAllowsArbitraryLoads`). It only exempts
`localhost`, `*.local`, and link-local addresses — the minimal correct scope.

**Diagnostic:** If the POST never shows up in uvicorn logs, ATS is blocking it.

---

### 2. Codable model doesn't match the wire shape

The most common iOS bug when switching from mock to live: the Swift `Decodable` structs
were written against a planned or outdated API shape, not the actual server response.

**Warning signs:**
- Error is `.decoding` (not `.transport`) — the request reached the server and got a 200
- `DecodingError.keyNotFound` — a required field is missing (e.g., `status` at wrong nesting level)
- `DecodingError.typeMismatch` — field exists but has the wrong type (e.g., `null` vs `String`)

**How to debug quickly:**
1. Add `#if DEBUG` logging in the decode catch block to print `String(describing: error)`
   and the first 500 chars of the raw response body.
2. Compare the printed JSON against the Swift CodingKeys character-by-character.
3. Check nesting — a flat Swift struct against a nested JSON object (or vice versa) is
   the most common mismatch.
4. Check optional vs required — a non-optional Swift property against a nullable JSON
   field will throw `keyNotFound` or `valueNotFound`.

**Key mapping gotcha (art-guide example):**
- Server sends `"score"` per candidate; Swift used `case confidence` (decoded "confidence").
- Fix: `case confidence = "score"`.

**Nested-envelope gotcha (art-guide example):**
- Server sends `{ "match": { "status": ..., "candidates": [...] } }` (nested).
- Old Swift model had `status` and `top_candidate` at the top level (flat).
- Fix: add an intermediate struct (`MatchEnvelope`) for the nested key, expose flat
  computed properties for backward compat with views.

---

### 3. xcconfig URL didn't propagate to the built app

xcconfig changes are NOT automatically reflected in a running app — you must clean-build.

**Verification:**
1. Print the resolved URL at app launch:
   ```swift
   print("[App] API base URL: \(AppConfig.apiBaseURL)")
   ```
2. Check the Xcode console immediately after launch — before tapping anything.
3. If the URL is the fallback (`https://api.invalid.local`) the xcconfig value didn't flow
   through to Info.plist or AppConfig isn't reading the right key.

**xcconfig gotcha:** `//` starts a comment in xcconfig. For HTTP URLs, escape it:
```
API_BASE_URL = http:/$()/localhost:8000
```
The `$()` expands to an empty string and prevents the parser from treating `//` as a
comment start.

**After any xcconfig edit:** ⌘⇧K (Clean Build Folder) then ⌘B.

---

## Diagnostic loop when "unexpected response" appears

```
1. Check uvicorn terminal when you tap "Try Again"
   → POST shows up  → network is fine; problem is Codable mismatch (issue #2)
   → No POST entry  → ATS is blocking HTTP (issue #1) or xcconfig gave wrong URL (issue #3)

2. If Codable mismatch:
   → Check Xcode console for the DecodingError print
   → Note the key path in the error (e.g., "No value for key 'status' in decoder")
   → Compare Swift CodingKeys to actual JSON keys

3. If wrong URL:
   → Check Xcode console for the launch-time URL print
   → If fallback URL, xcconfig didn't propagate → ⌘⇧K + ⌘B
```

## Apply all three preemptively on any new project

Doing all three upfront (ATS exception, model audit, URL print) is cheaper than
diagnosing them one at a time when live mode breaks.
