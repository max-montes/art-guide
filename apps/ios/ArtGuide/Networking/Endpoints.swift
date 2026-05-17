import Foundation

/// Path constants for the backend. Keep these in lockstep with
/// `services/api`. The backend owns versioning; bump the prefix here when
/// they bump it there.
enum Endpoints {
    static let apiVersion = "v1"

    /// `GET /healthz` — liveness probe; also warms the SigLIP model after
    /// a scale-to-zero cold start (D-028).
    static let healthz = "/healthz"

    /// `POST /v1/identify` — multipart upload of a photo.
    static let identify = "/\(apiVersion)/identify"

    /// `GET /v1/artworks/{id}` — refresh metadata for a known artwork id.
    static func artwork(id: String) -> String {
        "/\(apiVersion)/artworks/\(id)"
    }
}
