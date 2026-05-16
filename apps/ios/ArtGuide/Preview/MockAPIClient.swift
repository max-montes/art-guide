import Foundation
import UIKit
import os

/// `APIClientProtocol` implementation that returns canned responses
/// without ever hitting the network.
///
/// Two ways to drive what comes back:
///
///   1. Construct with a `MockScenario`. Use `.cycle` (the default in
///      previews) to round-robin through every status, or one of the
///      explicit cases (`.exact`, `.likely`, `.styleOnly`, `.noMatch`,
///      `.error`) to pin a single response.
///   2. Set `forcedResponse` or `forcedError` directly to override at
///      runtime — handy for a future dev menu.
///
/// The active scenario can also be selected via the
/// `ART_GUIDE_MOCK_SCENARIO` environment variable, which lets a tester
/// launch the simulator straight into, say, the `style_only` screen by
/// adding the variable to the scheme's Run configuration.
public final class MockAPIClient: APIClientProtocol, @unchecked Sendable {

    // MARK: - Scenario

    /// Maps to one of the canonical mocks, plus `.cycle` and `.error`.
    /// Raw values are snake_case to match the wire enum and to keep the
    /// env-var override readable (`ART_GUIDE_MOCK_SCENARIO=no_match`).
    public enum MockScenario: String, CaseIterable, Sendable {
        case cycle
        case exact
        case likely
        case styleOnly = "style_only"
        case noMatch = "no_match"
        case error

        /// Read the scenario the app should default to. Falls back to
        /// `.cycle` when the env var is missing or unrecognized so that
        /// existing previews continue to work.
        public static var fromEnvironment: MockScenario {
            guard let raw = ProcessInfo.processInfo.environment["ART_GUIDE_MOCK_SCENARIO"]?
                .trimmingCharacters(in: .whitespaces),
                  let scenario = MockScenario(rawValue: raw) else {
                return .cycle
            }
            return scenario
        }

        fileprivate var pinnedResponse: IdentifyResponse? {
            switch self {
            case .exact:     return MockData.exactIrises
            case .likely:    return MockData.likelyAmbiguous
            case .styleOnly: return MockData.styleOnlyImpressionist
            case .noMatch:   return MockData.noMatchScene
            case .cycle, .error: return nil
            }
        }
    }

    // MARK: - Public state

    /// Active scenario. Mutating this resets the cycle cursor so a tester
    /// can flip back to `.cycle` and start fresh from `exact`.
    public var scenario: MockScenario {
        didSet {
            cursorLock.withLock { $0 = 0 }
        }
    }

    /// If non-nil, every call returns this response (overrides scenario).
    public var forcedResponse: IdentifyResponse?

    /// If non-nil, every call throws this error (overrides everything).
    public var forcedError: APIError?

    /// Artificial delay so loading states are visible in the simulator.
    public var simulatedLatency: TimeInterval

    // MARK: - Private

    /// Protects the cycle cursor. `OSAllocatedUnfairLock` is async-safe
    /// (Swift 6 strict concurrency) unlike `NSLock`.
    private let cursorLock = OSAllocatedUnfairLock<Int>(initialState: 0)
    private let samples: [IdentifyResponse]

    // MARK: - Init

    public init(
        scenario: MockScenario = .fromEnvironment,
        samples: [IdentifyResponse] = MockData.allSamples,
        simulatedLatency: TimeInterval = 0.6,
        forcedResponse: IdentifyResponse? = nil,
        forcedError: APIError? = nil
    ) {
        self.scenario = scenario
        self.samples = samples.isEmpty ? [MockData.noMatchScene] : samples
        self.simulatedLatency = simulatedLatency
        self.forcedResponse = forcedResponse
        self.forcedError = forcedError
    }

    // MARK: - APIClientProtocol

    public func identify(image: UIImage) async throws -> IdentifyResponse {
        try await sleepIfNeeded()

        if let forcedError {
            throw forcedError
        }
        if let forcedResponse {
            return forcedResponse
        }

        switch scenario {
        case .error:
            throw APIError.transport("Mocked transport failure (scenario: error)")
        case .exact, .likely, .styleOnly, .noMatch:
            // Force-unwrap is safe: every non-cycle/non-error case returns a value.
            return scenario.pinnedResponse!
        case .cycle:
            let response = cursorLock.withLock { cursor -> IdentifyResponse in
                let r = samples[cursor % samples.count]
                cursor += 1
                return r
            }
            return response
        }
    }

    public func artwork(id: String) async throws -> ArtworkCandidate {
        try await sleepIfNeeded()

        if let forcedError {
            throw forcedError
        }

        // Pull the matching candidate out of the canned data, falling back
        // to the Irises example so previews always have something.
        let pool = samples.flatMap { [$0.topCandidate].compactMap { $0 } + $0.alternates }
        if let match = pool.first(where: { $0.id == id }) {
            return match
        }
        return MockData.exactIrises.topCandidate!
    }

    private func sleepIfNeeded() async throws {
        guard simulatedLatency > 0 else { return }
        let nanos = UInt64(simulatedLatency * 1_000_000_000)
        try await Task.sleep(nanoseconds: nanos)
    }
}
