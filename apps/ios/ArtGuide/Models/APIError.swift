import Foundation

/// Errors surfaced by `APIClient`. Use `userFacingMessage` when presenting
/// to the user — it formats each failure mode with actionable copy.
///
/// **Network errors** (`.networkUnreachable`, `.cannotFindHost`, etc.) are
/// mapped from `URLError` codes in `APIError.map(_:)`. Add new cases there
/// rather than catching raw `NSError` domain codes elsewhere.
public enum APIError: Error, Equatable, Sendable {

    // MARK: - Image/request errors

    /// The captured image, after compression, was still over the 10 MB cap.
    case payloadTooLarge(actualBytes: Int, maxBytes: Int)

    /// Could not encode the image as JPEG at all.
    case imageEncodingFailed

    /// Bad URL or could not build the request.
    case invalidRequest(String)

    // MARK: - Network-level errors (mapped from URLError)

    /// Device has no internet connectivity (NSURLError -1009).
    case networkUnreachable

    /// DNS lookup failed — host not found (NSURLError -1003).
    case cannotFindHost

    /// Host is reachable but refused the connection (NSURLError -1004).
    case cannotConnect

    /// Request timed out (NSURLError -1001). After the cold-start window
    /// this surfaces as "The server is waking up. Please try again in a moment."
    case timedOut

    /// TLS/SSL handshake or certificate validation failed.
    case tlsFailure(code: Int)

    /// Any other URLError that doesn't map to a specific case above.
    case transport(String, code: Int? = nil)

    // MARK: - HTTP-level errors

    /// Server returned a non-2xx status.
    case http(status: Int, message: String?)

    /// 429 with optional Retry-After hint (seconds).
    case rateLimited(retryAfterSeconds: Int?)

    /// Auth bearer was missing, expired, or rejected.
    case unauthorized

    // MARK: - Payload errors

    /// JSON didn't decode into the expected type.
    case decoding(String)

    /// Caller cancelled the request.
    case cancelled

    // MARK: - URLError → APIError mapping

    /// Maps a `URLError` to the most specific `APIError` case.
    /// Call this from `catch let urlErr as URLError` in `APIClient`.
    static func map(_ urlErr: URLError) -> APIError {
        switch urlErr.code {
        case .cancelled:
            return .cancelled
        case .notConnectedToInternet, .networkConnectionLost,
             .dataNotAllowed, .callIsActive:
            return .networkUnreachable
        case .cannotFindHost, .dnsLookupFailed:
            return .cannotFindHost
        case .cannotConnectToHost:
            return .cannotConnect
        case .timedOut:
            return .timedOut
        case .secureConnectionFailed,
             .serverCertificateHasBadDate,
             .serverCertificateUntrusted,
             .serverCertificateHasUnknownRoot,
             .serverCertificateNotYetValid,
             .clientCertificateRequired,
             .clientCertificateRejected:
            return .tlsFailure(code: urlErr.errorCode)
        default:
            return .transport(urlErr.localizedDescription, code: urlErr.errorCode)
        }
    }

    // MARK: - User-facing copy

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

        case .networkUnreachable:
            return "Check your internet connection."

        case .cannotFindHost, .cannotConnect:
            return "Can't reach the museum server right now."

        case .timedOut:
            return "The server is waking up. Please try again in a moment."

        case .tlsFailure(let code):
            #if DEBUG
            return "TLS/SSL failure (code \(code)). Check the server certificate."
            #else
            return "Couldn't establish a secure connection. Please try again."
            #endif

        case .transport(let detail, let code):
            #if DEBUG
            if let code {
                return "Network error (code \(code)): \(detail)"
            }
            return "Network error: \(detail)"
            #else
            _ = detail
            _ = code
            return "A network problem prevented the request. Please try again."
            #endif

        case .http(let status, let message):
            switch status {
            case 401, 403:
                return "Authentication problem — this is a bug, please report it."
            case 500...599:
                return "The museum server hit a problem — try again in a moment."
            default:
                #if DEBUG
                if let message, !message.isEmpty {
                    return "Server error (\(status)): \(message)"
                }
                return "Server error (\(status)). Please try again in a moment."
                #else
                _ = message
                return "Server error (\(status)). Please try again in a moment."
                #endif
            }

        case .rateLimited(let retryAfter):
            if let retryAfter, retryAfter > 0 {
                return "You're sending photos a bit fast. Try again in \(retryAfter)s."
            }
            return "You're sending photos a bit fast. Please wait a moment and try again."

        case .unauthorized:
            return "Authentication problem — this is a bug, please report it."

        case .decoding(let detail):
            #if DEBUG
            return "Got a confusing response from the server (decode error: \(detail))."
            #else
            _ = detail
            return "Got a confusing response from the server. Tap Try Again."
            #endif

        case .cancelled:
            return "Upload cancelled."
        }
    }
}
