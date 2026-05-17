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

    // MARK: - headline spot-checks (headline→case mapping must not regress)

    func test_headline_networkUnreachable() {
        XCTAssertEqual(APIError.networkUnreachable.headline, "No internet connection")
    }

    func test_headline_cannotFindHost() {
        XCTAssertEqual(APIError.cannotFindHost.headline, "Can't reach the museum")
    }

    func test_headline_cannotConnect() {
        XCTAssertEqual(APIError.cannotConnect.headline, "Can't reach the museum")
    }

    func test_headline_timedOut() {
        XCTAssertEqual(APIError.timedOut.headline, "The server is waking up…")
    }

    func test_headline_tlsFailure() {
        XCTAssertEqual(APIError.tlsFailure(code: -9807).headline, "Secure connection failed")
    }

    func test_headline_http401() {
        XCTAssertEqual(APIError.http(status: 401, message: nil).headline, "Authentication problem")
    }

    func test_headline_http503() {
        XCTAssertEqual(APIError.http(status: 503, message: nil).headline, "The museum server hit a problem")
    }

    func test_headline_decoding() {
        XCTAssertEqual(APIError.decoding("raw error text").headline, "Something went wrong")
    }

    func test_headline_transport_fallthrough() {
        XCTAssertEqual(APIError.transport("some error", code: -1).headline, "Network error")
    }

    // MARK: - debugDetail isolation (raw text must NOT appear in userFacingMessage)

    func test_decoding_userFacingMessage_isClean() {
        let raw = "DecodingError.dataCorrupted: Data was corrupted. NSDebugDescription=Unexpected character '<'"
        let err = APIError.decoding(raw)
        XCTAssertFalse(err.userFacingMessage.contains(raw),
            "Raw decode error text must not appear in userFacingMessage")
        XCTAssertEqual(err.userFacingMessage, "The server sent a response I couldn't read.")
    }

    func test_decoding_debugDetail_containsRawText() {
        let raw = "DecodingError.dataCorrupted: bad JSON"
        XCTAssertEqual(APIError.decoding(raw).debugDetail, raw)
    }
}
