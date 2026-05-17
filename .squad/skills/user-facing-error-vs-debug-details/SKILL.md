# Skill: User-Facing Error Copy vs. Debug Details

**First used:** 2026-05-16 (error screen polish follow-up to B.1)  
**Applies to:** Any SwiftUI error screen backed by a typed error enum

---

## Problem

When a typed error enum's `userFacingMessage` embeds raw debug text (via `#if DEBUG`), that text renders verbatim in the view body — turning a user-facing screen into a wall of NSError output. In debug builds this looks terrible; in release it's hidden but the same property serves two incompatible jobs (user copy and debug detail).

---

## Solution: Three-Property Pattern

Separate the error model into three distinct concerns:

| Property | Purpose | Audience | Build flavors |
|---|---|---|---|
| `headline: String` | One-line cause-specific title | User | All |
| `userFacingMessage: String` | Short actionable body | User | All |
| `debugDetail: String?` | Raw technical detail | Developer | DEBUG only (at render layer) |

**Key constraint:** `userFacingMessage` must return clean copy in ALL build flavors. Never put `#if DEBUG` blocks inside it. Debug logic belongs at the *render layer* (the View), not in the model.

---

## Implementation

### In the Error Enum

```swift
public enum APIError: Error {

    // MARK: - Per-case headline
    public var headline: String {
        switch self {
        case .networkUnreachable:  return "No internet connection"
        case .cannotFindHost, .cannotConnect: return "Can't reach the museum"
        case .timedOut:            return "The server is waking up…"
        case .tlsFailure:          return "Secure connection failed"
        case .decoding:            return "Something went wrong"
        case .http(let s, _) where 500...599 ~= s: return "The museum server hit a problem"
        // …etc
        }
    }

    // MARK: - Raw debug detail (nil when nothing specific to show)
    public var debugDetail: String? {
        switch self {
        case .decoding(let detail):        return detail
        case .transport(let d, let c):     return c.map { "URLError \($0): \(d)" } ?? d
        case .tlsFailure(let code):        return "TLS code: \(code)"
        case .http(let s, let msg?):       return "HTTP \(s): \(msg)"
        default:                           return nil
        }
    }

    // MARK: - User-facing body (ALWAYS clean — no #if DEBUG here)
    public var userFacingMessage: String {
        switch self {
        case .decoding:    return "The server sent a response I couldn't read."
        case .timedOut:    return "The server is waking up. Please try again in a moment."
        // …etc — no raw error text, ever
        }
    }
}
```

### In the View (render layer owns the #if DEBUG)

```swift
private struct ErrorView: View {
    let error: APIError
    @State private var detailsExpanded = false

    var body: some View {
        VStack(spacing: 24) {
            Text(error.headline)          // cause-specific title
                .font(.title3.weight(.semibold))
            Text(error.userFacingMessage) // always clean body
                .foregroundStyle(.secondary)

            #if DEBUG
            if let detail = error.debugDetail {
                DisclosureGroup("Details", isExpanded: $detailsExpanded) {
                    Text(detail)
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                }
            }
            #endif

            Button("Try Again", action: onRetry)
                .buttonStyle(.borderedProminent)
        }
    }
}
```

---

## Headline Mapping Rules

- Each error case gets a distinct headline reflecting its **cause**, not the symptom.
- `"Couldn't identify that photo"` is reserved for genuine `no_match` from a successful 200 response — **never** reuse it for network or decode errors.
- Keep headlines short (3–5 words). Actionable copy goes in `userFacingMessage`.

---

## Testing the Invariant

Add tests that assert raw text does NOT leak into `userFacingMessage`:

```swift
func test_decoding_userFacingMessage_isClean() {
    let raw = "DecodingError.dataCorrupted: ..."
    let err = APIError.decoding(raw)
    XCTAssertFalse(err.userFacingMessage.contains(raw))
}

func test_decoding_debugDetail_containsRawText() {
    let raw = "DecodingError.dataCorrupted: bad JSON"
    XCTAssertEqual(APIError.decoding(raw).debugDetail, raw)
}
```

Also test `headline` for each major case — this prevents future enum additions from accidentally falling through to the wrong title.

---

## What NOT to Do

- Don't put `#if DEBUG` inside `userFacingMessage` — the model should not know about build configuration.
- Don't use a single headline ("Couldn't identify that photo") for all error states — it's misleading for network errors.
- Don't show `debugDetail` text to users in release builds — use `#if DEBUG` at the view layer.
- Don't skip `debugDetail` tests — regression is likely when new error cases are added.
