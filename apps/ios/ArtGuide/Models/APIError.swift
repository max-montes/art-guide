import Foundation

/// Errors surfaced by `APIClient`. Use `userFacingMessage` when presenting
/// to the user — it formats Retry-After and known status codes nicely.
public enum APIError: Error, Equatable, Sendable {

    /// The captured image, after compression, was still over the 10 MB cap.
    case payloadTooLarge(actualBytes: Int, maxBytes: Int)

    /// Could not encode the image as JPEG at all.
    case imageEncodingFailed

    /// Bad URL or could not build the request.
    case invalidRequest(String)

    /// Server returned a non-2xx status.
    case http(status: Int, message: String?)

    /// 429 with optional Retry-After hint (seconds).
    case rateLimited(retryAfterSeconds: Int?)

    /// Auth bearer was missing, expired, or rejected.
    case unauthorized

    /// Underlying transport failure (network down, TLS, etc).
    case transport(String)

    /// JSON didn't decode into IdentifyResponse.
    case decoding(String)

    /// Caller cancelled the request.
    case cancelled

    public var userFacingMessage: String {
        switch self {
        case .payloadTooLarge(let actual, let max):
            let mb = Double(actual) / 1_048_576
            let cap = Double(max) / 1_048_576
            return String(format: "Photo is too large to upload (%.1f MB, limit %.0f MB). Try a smaller image.", mb, cap)
        case .imageEncodingFailed:
            return "Could not prepare that photo for upload. Try retaking it."
        case .invalidRequest:
            return "Something went wrong building the request. Please try again."
        case .http(let status, let message):
            if let message, !message.isEmpty {
                return "Server error (\(status)): \(message)"
            }
            return "Server error (\(status)). Please try again in a moment."
        case .rateLimited(let retryAfter):
            if let retryAfter, retryAfter > 0 {
                return "You're sending photos a bit fast. Try again in \(retryAfter)s."
            }
            return "You're sending photos a bit fast. Please wait a moment and try again."
        case .unauthorized:
            return "This build isn't authorized to talk to the API. Check the API key in Config.xcconfig."
        case .transport(let detail):
            return "Network problem: \(detail)"
        case .decoding(let detail):
            #if DEBUG
            return "Decode error (check Xcode console): \(detail)"
            #else
            return "Got an unexpected response from the server. Please try again."
            #endif
        case .cancelled:
            return "Upload cancelled."
        }
    }
}
