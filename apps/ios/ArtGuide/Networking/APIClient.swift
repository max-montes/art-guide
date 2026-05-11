import Foundation
import UIKit

/// Abstraction over the backend so views/view-models can be wired to a
/// real `APIClient` or to `MockAPIClient` for previews and offline dev.
public protocol APIClientProtocol: AnyObject, Sendable {
    /// Upload a captured photo and return the parsed identify response.
    /// Throws `APIError` for all failure modes.
    func identify(image: UIImage) async throws -> IdentifyResponse

    /// Refresh metadata for a single artwork by id.
    func artwork(id: String) async throws -> ArtworkCandidate
}

/// Real network client. Talks to `AppConfig.apiBaseURL`, attaches the
/// bearer token from `AppConfig.apiKey`, and uses `URLSession` with
/// `async/await`. No third-party dependencies.
final class APIClient: APIClientProtocol, @unchecked Sendable {

    /// Hard cap defined in the prompt: server will reject anything bigger.
    static let maxUploadBytes = 10 * 1024 * 1024

    /// Long-edge resize target before re-encoding to JPEG.
    static let maxLongEdgePixels: CGFloat = 1600

    /// JPEG re-encoding quality.
    static let jpegQuality: CGFloat = 0.85

    private let session: URLSession
    private let baseURL: URL
    private let apiKey: String

    init(
        baseURL: URL = AppConfig.apiBaseURL,
        apiKey: String = AppConfig.apiKey,
        session: URLSession = .shared
    ) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.session = session
    }

    // MARK: - identify

    func identify(image: UIImage) async throws -> IdentifyResponse {
        let jpeg = try Self.compress(image: image)
        let url = baseURL.appendingPathComponent(Endpoints.identify)

        let boundary = "ArtGuideBoundary-\(UUID().uuidString)"
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let body = Self.multipartBody(jpeg: jpeg, boundary: boundary, filename: "capture.jpg")
        request.httpBody = body

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: request)
        } catch is CancellationError {
            throw APIError.cancelled
        } catch let urlErr as URLError where urlErr.code == .cancelled {
            throw APIError.cancelled
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("No HTTP response")
        }

        try Self.checkStatus(http: http, data: data)

        do {
            let decoder = JSONDecoder()
            return try decoder.decode(IdentifyResponse.self, from: data)
        } catch {
            #if DEBUG
            print("[ArtGuide] ⚠️ Decode error (identify): \(error)")
            if let raw = String(data: data, encoding: .utf8) {
                print("[ArtGuide]   Raw response body: \(raw.prefix(500))")
            }
            #endif
            throw APIError.decoding(String(describing: error))
        }
    }

    // MARK: - artwork refresh

    func artwork(id: String) async throws -> ArtworkCandidate {
        let url = baseURL.appendingPathComponent(Endpoints.artwork(id: id))
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: request)
        } catch is CancellationError {
            throw APIError.cancelled
        } catch let urlErr as URLError where urlErr.code == .cancelled {
            throw APIError.cancelled
        } catch {
            throw APIError.transport(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError.transport("No HTTP response")
        }
        try Self.checkStatus(http: http, data: data)

        do {
            return try JSONDecoder().decode(ArtworkCandidate.self, from: data)
        } catch {
            #if DEBUG
            print("[ArtGuide] ⚠️ Decode error (artwork): \(error)")
            #endif
            throw APIError.decoding(String(describing: error))
        }
    }

    // MARK: - HTTP helpers

    private static func checkStatus(http: HTTPURLResponse, data: Data) throws {
        switch http.statusCode {
        case 200..<300:
            return
        case 401, 403:
            throw APIError.unauthorized
        case 429:
            let info = RateLimitInfo(response: http)
            throw APIError.rateLimited(retryAfterSeconds: info.retryAfterSeconds)
        default:
            let message = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            throw APIError.http(status: http.statusCode, message: message)
        }
    }

    // MARK: - Image compression

    /// Resizes the image so its long edge is `<= maxLongEdgePixels`, then
    /// re-encodes as JPEG at `jpegQuality`. Throws if the result still
    /// exceeds `maxUploadBytes`.
    ///
    /// EXIF stripping note: `UIImage.jpegData(compressionQuality:)` does
    /// **not** copy EXIF (or GPS) metadata from the source `CGImageSource`
    /// — the encoded bytes only carry pixel data plus the JPEG markers
    /// Apple writes by default. The `UIGraphicsImageRenderer` in
    /// `resize(_:maxLongEdge:)` produces a fresh `CGImage` from a draw
    /// pass, which has no metadata to begin with. The combination
    /// satisfies the "strip all EXIF, including GPS" rule from
    /// `docs/image-pipeline.md` without us having to round-trip through
    /// `CGImageDestination`. If we ever switch to `HEIC` or any encoder
    /// that preserves source metadata, revisit this.
    static func compress(image: UIImage) throws -> Data {
        let resized = resize(image: image, maxLongEdge: maxLongEdgePixels)
        guard let data = resized.jpegData(compressionQuality: jpegQuality) else {
            throw APIError.imageEncodingFailed
        }
        if data.count > maxUploadBytes {
            throw APIError.payloadTooLarge(actualBytes: data.count, maxBytes: maxUploadBytes)
        }
        return data
    }

    static func resize(image: UIImage, maxLongEdge: CGFloat) -> UIImage {
        let size = image.size
        let longEdge = max(size.width, size.height)
        guard longEdge > maxLongEdge, longEdge > 0 else {
            return image
        }
        let scale = maxLongEdge / longEdge
        let newSize = CGSize(width: floor(size.width * scale),
                             height: floor(size.height * scale))

        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        format.opaque = true
        let renderer = UIGraphicsImageRenderer(size: newSize, format: format)
        return renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: newSize))
        }
    }

    // MARK: - Multipart

    static func multipartBody(jpeg: Data, boundary: String, filename: String) -> Data {
        var body = Data()
        let line = "\r\n"

        body.append("--\(boundary)\(line)".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"image\"; filename=\"\(filename)\"\(line)".data(using: .utf8)!)
        body.append("Content-Type: image/jpeg\(line)\(line)".data(using: .utf8)!)
        body.append(jpeg)
        body.append(line.data(using: .utf8)!)
        body.append("--\(boundary)--\(line)".data(using: .utf8)!)
        return body
    }
}
