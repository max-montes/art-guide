import SwiftUI

/// Top-level navigation. Drives:
///   capture  → loading  → result
/// using a single `Phase` enum so transitions stay explicit.
struct RootView: View {

    @EnvironmentObject private var session: AppSession
    @State private var phase: Phase = .capture
    /// Secondary cold-start message shown beneath the loading spinner (B.2).
    @State private var loadingMessage: String?

    enum Phase: Equatable {
        case capture
        case loading(progress: Double)
        case result(IdentifyResponse)
        case failure(APIError)
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("ArtGuide")
                .navigationBarTitleDisplayMode(.inline)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch phase {
        case .capture:
            CaptureView { image in
                Task { await identify(image: image) }
            }
            .onAppear {
                // D-028: warm the container as soon as the capture screen
                // appears. By the time the user snaps a photo and taps
                // identify, SigLIP is loaded and the cold-start penalty is
                // already paid. Failure is silently ignored — warmup must
                // never block identify.
                Task { await session.client.warmup() }
            }

        case .loading(let progress):
            LoadingView(progress: progress, statusMessage: loadingMessage) {
                phase = .capture
            }

        case .result(let response):
            ResultView(response: response) {
                phase = .capture
            }

        case .failure(let error):
            ErrorView(error: error) {
                phase = .capture
            }
        }
    }

    private func identify(image: UIImage) async {
        loadingMessage = nil
        phase = .loading(progress: 0.1)

        // B.2: escalating cold-start messages on a background task.
        // Each timer fires at the threshold and updates `loadingMessage`;
        // the task is cancelled (via `identifyTask`) when identify completes.
        let messageTask = Task {
            do {
                try await Task.sleep(for: .seconds(LoadingMessageThreshold.warmingUp))
                await MainActor.run { loadingMessage = "Waking up the museum…" }
                try await Task.sleep(for: .seconds(
                    LoadingMessageThreshold.almostReady - LoadingMessageThreshold.warmingUp
                ))
                await MainActor.run {
                    loadingMessage = "Almost ready — first match takes a bit longer…"
                }
                try await Task.sleep(for: .seconds(
                    LoadingMessageThreshold.stillWorking - LoadingMessageThreshold.almostReady
                ))
                await MainActor.run {
                    loadingMessage = "Still working on it — feel free to keep the camera steady…"
                }
            } catch {
                // Task cancelled — identify finished before the threshold fired.
            }
        }

        defer { messageTask.cancel() }

        do {
            phase = .loading(progress: 0.4)
            let response = try await session.client.identify(image: image)
            session.lastResponse = response
            loadingMessage = nil
            phase = .result(response)
        } catch let apiErr as APIError {
            session.lastError = apiErr
            loadingMessage = nil
            phase = .failure(apiErr)
        } catch {
            let wrapped = APIError.transport(error.localizedDescription)
            session.lastError = wrapped
            loadingMessage = nil
            phase = .failure(wrapped)
        }
    }
}

/// Failure screen. Headline and body are driven by `APIError.headline` /
/// `APIError.userFacingMessage` so each error case gets its own title rather
/// than the generic "Couldn't identify that photo" (which is only correct for
/// genuine `no_match` from a 200 response).
///
/// In DEBUG builds a collapsible "Details" disclosure shows `debugDetail` for
/// rapid diagnosis. In release builds the disclosure is hidden entirely.
private struct ErrorView: View {
    let error: APIError
    let onRetry: () -> Void

    @State private var detailsExpanded = false

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.secondary)

            Text(error.headline)
                .font(.title3.weight(.semibold))

            Text(error.userFacingMessage)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal)

            #if DEBUG
            if let detail = error.debugDetail {
                DisclosureGroup("Details", isExpanded: $detailsExpanded) {
                    Text(detail)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 4)
                }
                .padding(.horizontal)
            }
            #endif

            Button("Try Again", action: onRetry)
                .buttonStyle(.borderedProminent)
        }
        .padding()
    }
}

#Preview("Root — capture") {
    RootView()
        .environmentObject(AppSession(client: MockAPIClient()))
}
