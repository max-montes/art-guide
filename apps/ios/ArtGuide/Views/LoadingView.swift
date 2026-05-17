import SwiftUI

/// Thresholds (seconds) at which the loading status message escalates during
/// a slow identify request. Define them here so they're easy to tune.
enum LoadingMessageThreshold {
    /// Show "Waking up the museum…" after this many seconds with no response.
    static let warmingUp: TimeInterval    = 3
    /// Show "Almost ready…" after this many seconds.
    static let almostReady: TimeInterval  = 10
    /// Show "Still working on it…" after this many seconds.
    static let stillWorking: TimeInterval = 25
}

/// Indeterminate-friendly upload screen with a cancel button.
///
/// `progress` is best-effort. When we don't have real upload progress
/// (URLSession's high-level `data(for:)` doesn't expose it) we still
/// animate a `ProgressView` so the screen feels alive.
///
/// `statusMessage` is the cold-start escalation message (B.2). It appears
/// beneath the spinner and changes on a timer in `RootView.identify()`.
/// Passing `nil` hides the secondary row entirely.
struct LoadingView: View {

    let progress: Double
    let statusMessage: String?
    let onCancel: () -> Void

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            ProgressView(value: clampedProgress)
                .progressViewStyle(.linear)
                .padding(.horizontal, 48)

            Text("Identifying artwork…")
                .font(.headline)
            Text("This usually takes a few seconds.")
                .font(.footnote)
                .foregroundStyle(.secondary)

            if let msg = statusMessage {
                Text(msg)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)
                    .transition(.opacity)
            }

            Spacer()

            Button("Cancel", role: .cancel, action: onCancel)
                .buttonStyle(.bordered)
                .controlSize(.large)
                .padding(.bottom, 24)
        }
        .padding(.horizontal)
        .animation(.easeInOut(duration: 0.4), value: statusMessage)
    }

    private var clampedProgress: Double {
        min(max(progress, 0.05), 0.95)
    }
}

#Preview("Loading — no message") {
    LoadingView(progress: 0.4, statusMessage: nil, onCancel: {})
}

#Preview("Loading — waking up") {
    LoadingView(progress: 0.4,
                statusMessage: "Waking up the museum…",
                onCancel: {})
}

#Preview("Loading — almost ready") {
    LoadingView(progress: 0.6,
                statusMessage: "Almost ready — first match takes a bit longer…",
                onCancel: {})
}
