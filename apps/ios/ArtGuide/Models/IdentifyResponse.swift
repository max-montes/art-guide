import Foundation

/// Top-level response from `POST /v1/identify`.
///
/// Decodes the actual server wire format (per `docs/api.md`):
/// ```json
/// {
///   "request_id": "srv_...",
///   "match": { "status", "confidence", "candidates": [...] },
///   "explanation": { "text", "tone", "length", "grounded_fields", "hedged" },
///   "diagnostics": { ... }
/// }
/// ```
///
/// Computed properties (`status`, `topCandidate`, `alternates`, `disclaimer`)
/// preserve the view/preview surface so no view files need to change.
public struct IdentifyResponse: Codable, Hashable, Sendable {

    // MARK: - Stored (matches server shape)

    public let requestID: String?
    /// The `match` envelope containing status, overall confidence, and candidates.
    public let match: MatchEnvelope
    /// LLM explanation grounded in retrieved metadata.
    public let explanation: Explanation?

    // MARK: - Computed view accessors

    /// Overall match status — drives which `ResultView` sub-view is shown.
    public var status: MatchStatus { match.status }

    /// First (highest-scoring) candidate; the primary artwork shown to the user.
    public var topCandidate: ArtworkCandidate? { match.candidates.first }

    /// Remaining candidates shown under a `likely` result.
    public var alternates: [ArtworkCandidate] { Array(match.candidates.dropFirst()) }

    /// UI-generated disclaimer for hedged statuses. Not a server field;
    /// derived from `match.status` + `explanation.hedged`.
    public var disclaimer: String? {
        guard explanation?.hedged == true else { return nil }
        switch match.status {
        case .likely:    return "Identification is uncertain — a few similar works also matched."
        case .styleOnly: return "Style guidance only — no specific artwork was matched."
        default:         return nil
        }
    }

    /// Style guidance placeholder. The v1 API does not send this field;
    /// always `nil` on live responses. Retained for preview compatibility.
    public var style: StyleGuidance? { nil }

    // MARK: - CodingKeys (server shape only)

    private enum CodingKeys: String, CodingKey {
        case requestID = "request_id"
        case match
        case explanation
        // `diagnostics` is present on the wire but not needed in the app yet
    }

    // MARK: - Memberwise init (used by MockData and unit tests)

    public init(
        requestID: String? = nil,
        status: MatchStatus,
        topCandidate: ArtworkCandidate? = nil,
        alternates: [ArtworkCandidate] = [],
        style: StyleGuidance? = nil,       // accepted but ignored; v1 API has no `style` field
        explanation: Explanation? = nil,
        disclaimer: String? = nil          // accepted but ignored; computed from hedged + status
    ) {
        self.requestID = requestID
        var candidates: [ArtworkCandidate] = []
        if let top = topCandidate { candidates.append(top) }
        candidates.append(contentsOf: alternates)
        self.match = MatchEnvelope(
            status: status,
            confidence: topCandidate?.confidence,
            candidates: candidates
        )
        self.explanation = explanation
    }
}

// MARK: - MatchEnvelope

/// The `match` object inside `POST /v1/identify` response.
public struct MatchEnvelope: Codable, Hashable, Sendable {
    /// Overall status — drives the result view.
    public let status: MatchStatus
    /// Overall confidence in [0, 1].
    public let confidence: Double?
    /// Ranked candidates. Empty iff `status == .noMatch`.
    public let candidates: [ArtworkCandidate]

    public init(
        status: MatchStatus,
        confidence: Double? = nil,
        candidates: [ArtworkCandidate] = []
    ) {
        self.status = status
        self.confidence = confidence
        self.candidates = candidates
    }
}
