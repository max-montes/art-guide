import SwiftUI

@main
struct ArtGuideApp: App {

    /// Swap `MockAPIClient()` for `APIClient()` to hit the real backend.
    /// See README → "Switching between mock and real API".
    @StateObject private var session = AppSession(client: APIClient())

    init() {
        #if DEBUG
        print("[ArtGuide] API base URL: \(AppConfig.apiBaseURL)")
        print("[ArtGuide] API key is placeholder: \(AppConfig.isUsingPlaceholderKey)")
        #endif
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(session)
        }
    }
}

/// App-level state container. Holds the active `APIClientProtocol` and
/// the result of the most recent identify call. Kept deliberately small;
/// per-screen state lives in the views themselves.
@MainActor
final class AppSession: ObservableObject {
    let client: APIClientProtocol

    @Published var lastResponse: IdentifyResponse?
    @Published var lastError: APIError?

    init(client: APIClientProtocol) {
        self.client = client
    }
}
