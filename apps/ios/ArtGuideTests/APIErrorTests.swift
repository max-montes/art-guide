import XCTest
@testable import ArtGuide

/// Unit tests for `APIError.map(_:)` — verifies that `URLError` codes are
/// translated into the correct typed `APIError` cases. No networking involved.
final class APIErrorTests: XCTestCase {

    // MARK: - URLError → APIError mapping

    func test_map_notConnectedToInternet_returnsNetworkUnreachable() {
        let urlErr = URLError(.notConnectedToInternet)
        XCTAssertEqual(APIError.map(urlErr), .networkUnreachable)
    }

    func test_map_networkConnectionLost_returnsNetworkUnreachable() {
        let urlErr = URLError(.networkConnectionLost)
        XCTAssertEqual(APIError.map(urlErr), .networkUnreachable)
    }

    func test_map_cannotFindHost_returnsCannotFindHost() {
        let urlErr = URLError(.cannotFindHost)
        XCTAssertEqual(APIError.map(urlErr), .cannotFindHost)
    }

    func test_map_dnsLookupFailed_returnsCannotFindHost() {
        let urlErr = URLError(.dnsLookupFailed)
        XCTAssertEqual(APIError.map(urlErr), .cannotFindHost)
    }

    func test_map_cannotConnectToHost_returnsCannotConnect() {
        let urlErr = URLError(.cannotConnectToHost)
        XCTAssertEqual(APIError.map(urlErr), .cannotConnect)
    }

    func test_map_timedOut_returnsTimedOut() {
        let urlErr = URLError(.timedOut)
        XCTAssertEqual(APIError.map(urlErr), .timedOut)
    }

    func test_map_cancelled_returnsCancelled() {
        let urlErr = URLError(.cancelled)
        XCTAssertEqual(APIError.map(urlErr), .cancelled)
    }

    func test_map_secureConnectionFailed_returnsTLSFailure() {
        let urlErr = URLError(.secureConnectionFailed)
        let result = APIError.map(urlErr)
        if case .tlsFailure(let code) = result {
            XCTAssertEqual(code, urlErr.errorCode)
        } else {
            XCTFail("Expected .tlsFailure, got \(result)")
        }
    }

    func test_map_serverCertificateUntrusted_returnsTLSFailure() {
        let urlErr = URLError(.serverCertificateUntrusted)
        let result = APIError.map(urlErr)
        if case .tlsFailure = result { /* pass */ } else {
            XCTFail("Expected .tlsFailure, got \(result)")
        }
    }

    func test_map_unknownCode_returnsTransport() {
        // Use a rarely-seen code that won't match any specific case.
        let urlErr = URLError(.backgroundSessionInUseByAnotherProcess)
        let result = APIError.map(urlErr)
        if case .transport = result { /* pass */ } else {
            XCTFail("Expected .transport fallthrough, got \(result)")
        }
    }

    // MARK: - userFacingMessage spot-checks

    func test_userFacingMessage_networkUnreachable() {
        XCTAssertEqual(
            APIError.networkUnreachable.userFacingMessage,
            "Check your internet connection."
        )
    }

    func test_userFacingMessage_timedOut() {
        XCTAssertEqual(
            APIError.timedOut.userFacingMessage,
            "The server is waking up. Please try again in a moment."
        )
    }

    func test_userFacingMessage_cannotFindHost() {
        XCTAssertEqual(
            APIError.cannotFindHost.userFacingMessage,
            "Can't reach the museum server right now."
        )
    }

    func test_userFacingMessage_http5xx() {
        let err = APIError.http(status: 503, message: nil)
        XCTAssertEqual(
            err.userFacingMessage,
            "The museum server hit a problem — try again in a moment."
        )
    }

    func test_userFacingMessage_cancelled() {
        XCTAssertEqual(APIError.cancelled.userFacingMessage, "Upload cancelled.")
    }
}
