import Foundation

// TODO: sync with packages/shared/api/identify-response.schema.json

/// One artwork suggested by the retrieval pipeline.
///
/// Field names mirror the normalized record described in
/// `docs/data-model.md`. Most fields are optional because museum sources
/// are inconsistent: `style_only` results, for instance, may carry only
/// `period`, `culture`, and `medium`. The view layer is responsible for
/// hiding any field that is `nil`.
public struct ArtworkCandidate: Codable, Identifiable, Hashable, Sendable {
    public let id: String
    public let title: String?
    public let artist: String?
    public let date: String?
    public let medium: String?
    public let culture: String?
    public let period: String?
    public let museum: String?
    public let sourceURL: URL?
    public let imageURL: URL?
    /// Model confidence in [0, 1]. Drives ConfidenceBadge color.
    public let confidence: Double

    enum CodingKeys: String, CodingKey {
        case id
        case title
        case artist
        case date
        case medium
        case culture
        case period
        case museum
        case sourceURL = "source_url"
        case imageURL = "image_url"
        case confidence = "score"   // server sends "score"; stored locally as confidence
    }

    public init(
        id: String,
        title: String? = nil,
        artist: String? = nil,
        date: String? = nil,
        medium: String? = nil,
        culture: String? = nil,
        period: String? = nil,
        museum: String? = nil,
        sourceURL: URL? = nil,
        imageURL: URL? = nil,
        confidence: Double
    ) {
        self.id = id
        self.title = title
        self.artist = artist
        self.date = date
        self.medium = medium
        self.culture = culture
        self.period = period
        self.museum = museum
        self.sourceURL = sourceURL
        self.imageURL = imageURL
        self.confidence = confidence
    }
}
