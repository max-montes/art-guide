import SwiftUI

/// Top-level navigation. Drives:
///   capture  → loading  → result
/// using a single `Phase` enum so transitions stay explicit.
struct RootView: View {

    @EnvironmentObject private var session: AppSession
    @State private var phase: Phase = .capture

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
            LoadingView(progress: progress) {
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
        phase = .loading(progress: 0.1)
        do {
            // We don't have first-class progress callbacks on
            // URLSession.data(for:) — show indeterminate-feeling progress.
            phase = .loading(progress: 0.4)
            let response = try await session.client.identify(image: image)
            session.lastResponse = response
            phase = .result(response)
        } catch let apiErr as APIError {
            session.lastError = apiErr
            phase = .failure(apiErr)
        } catch {
            let wrapped = APIError.transport(error.localizedDescription)
            session.lastError = wrapped
            phase = .failure(wrapped)
        }
    }
}

/// Minimal failure presentation. Lives here to keep `RootView` self-contained;
/// promote to its own file if it ever grows.
private struct ErrorView: View {
    let error: APIError
    let onRetry: () -> Void

    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 56, weight: .light))
                .foregroundStyle(.secondary)
            Text("Couldn't identify that photo")
                .font(.title3.weight(.semibold))
            Text(error.userFacingMessage)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal)
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
