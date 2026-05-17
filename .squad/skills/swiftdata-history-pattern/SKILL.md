# Skill: SwiftData History Pattern

**Confidence:** very high
**First used:** D-037 (ios-engineer, 2026-05-16)
**Extended:** ios-engineer-4 (2026-05-17) — original + thumbnail dual storage
**Applies to:** Any SwiftUI + SwiftData local history feature

---

## Problem

You need a local, device-only history list: save records after each operation, list them newest-first, support swipe-delete, and tap-to-detail. You want native SwiftUI integration with minimal boilerplate.

---

## Solution

### 1. Model

```swift
import SwiftData

@Model
final class HistoryEntry {
    var id: UUID
    var timestamp: Date
    var thumbnailData: Data?   // optional — entry still useful without it
    var status: String         // raw value, not enum — survives case renames without migration
    var rawResponseJSON: Data  // full response; decode on detail tap

    init(...) { ... }
}
```

**Tips:**
- Store enums as `String` (raw value) to survive Swift renames without a migration.
- Make heavy blobs (`thumbnailData`, `rawResponseJSON`) optional or flag-guarded so missing data doesn't corrupt the row.
- Storing the full response JSON is the cleanest way to re-render a detail view without network — no duplication of field storage.

---

### 2. ModelContainer setup

In `App.init()` (not `body`) to avoid re-creation on view rebuild:

```swift
import SwiftData

@main
struct MyApp: App {
    private let modelContainer: ModelContainer

    init() {
        do {
            modelContainer = try ModelContainer(for: HistoryEntry.self)
        } catch {
            fatalError("SwiftData ModelContainer failed: \(error)")
        }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .modelContainer(modelContainer)
        }
    }
}
```

**Why `fatalError`?** A failure here means data corruption or schema migration failure — surfacing it loudly is appropriate. Users can delete-and-reinstall.

---

### 3. Save flow (fire-and-forget after success)

Always call from `@MainActor` context. Background work (thumbnail) via `Task.detached`:

```swift
@MainActor
private func saveToHistory(image: UIImage, response: IdentifyResponse) async {
    // Heavy CPU work off main
    let thumbData = await Task.detached(priority: .userInitiated) {
        ThumbnailGenerator.generate(from: image)
    }.value

    guard let jsonData = try? JSONEncoder().encode(response) else {
        print("[History] JSON encode failed")
        return
    }

    let entry = HistoryEntry(
        thumbnailData: thumbData,
        status: response.status.rawValue,
        rawResponseJSON: jsonData
    )
    modelContext.insert(entry)
    // SwiftData auto-saves on next run loop turn; explicit save optional
}
```

Call with `Task { await saveToHistory(...) }` — fire-and-forget. Errors must never propagate to the result screen.

---

### 4. List view

```swift
struct HistoryView: View {
    @Environment(\.modelContext) private var modelContext
    @Query(sort: \HistoryEntry.timestamp, order: .reverse) private var entries: [HistoryEntry]

    var body: some View {
        List {
            ForEach(entries) { entry in
                NavigationLink { DetailView(entry: entry) } label: { RowView(entry: entry) }
            }
            .onDelete(perform: delete)
        }
    }

    private func delete(at offsets: IndexSet) {
        for i in offsets { modelContext.delete(entries[i]) }
    }
}
```

---

### 5. Thumbnail generation (pixel space)

`UIGraphicsImageRenderer` renders at the screen scale by default (3× on Retina). Always force `format.scale = 1.0` to produce a predictable pixel count:

```swift
enum ThumbnailGenerator {
    static let maxLongEdge: CGFloat = 512
    static let jpegQuality: CGFloat = 0.7

    static func generate(from image: UIImage) -> Data? {
        downscale(image).jpegData(compressionQuality: jpegQuality)
    }

    private static func downscale(_ image: UIImage) -> UIImage {
        let px = CGSize(
            width:  image.size.width  * image.scale,
            height: image.size.height * image.scale
        )
        let longEdge = max(px.width, px.height)
        guard longEdge > maxLongEdge else { return image }

        let s = maxLongEdge / longEdge
        let newSize = CGSize(width: (px.width * s).rounded(), height: (px.height * s).rounded())

        let fmt = UIGraphicsImageRendererFormat(); fmt.scale = 1.0
        return UIGraphicsImageRenderer(size: newSize, format: fmt).image { _ in
            image.draw(in: CGRect(origin: .zero, size: newSize))
        }
    }
}
```

**Tests:** `UIImage(data:)` decoded from JPEG has `scale=1`, so `thumb.size` == pixel dimensions. Compare against `maxLongEdge` directly (no multiplication needed).

---

### 6. Re-rendering detail from JSON

```swift
struct ResultDetailView: View {
    let entry: HistoryEntry
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        if let response = try? JSONDecoder().decode(IdentifyResponse.self, from: entry.rawResponseJSON) {
            ResultView(response: response) { dismiss() }
        } else {
            // fallback / decoding error view
        }
    }
}
```

No separate model storage — `rawResponseJSON` is the source of truth.

---

### 7. Previews with in-memory container

```swift
#Preview {
    NavigationStack { HistoryView() }
        .modelContainer(for: HistoryEntry.self, inMemory: true)
}
```

Never use the on-disk container in previews — multiple preview processes opening the same SQLite file causes crashes.

---

### 8. RelativeDateTimeFormatter

```swift
private extension Date {
    var relativeFormatted: String {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f.localizedString(for: self, relativeTo: Date())
    }
}
```

Produces: "2 hr. ago", "yesterday", "3 days ago". `unitsStyle = .full` gives "2 hours ago" (wordier).

---

## Gotchas

| Gotcha | Fix |
|---|---|
| `UIGraphicsImageRenderer` outputs 3× pixels at 3× scale | Always set `format.scale = 1.0` |
| `ModelContext` is `@MainActor` | Mark save func `@MainActor`; use `Task.detached` only for CPU work |
| Previews crash with on-disk container | Use `.modelContainer(for: ..., inMemory: true)` |
| `@Model` stored enum breaks migration on rename | Store as `String` (raw value), not enum type |
| Retroactive `UIImage: Identifiable` for `fullScreenCover(item:)` | Wrap in a small `struct IdentifiableImage: Identifiable { let id = UUID(); let image: UIImage }` |
| Storing raw camera bytes balloons the store | Re-encode to JPEG at a sane cap (see Extension 1) |

---

## Extension 1 — Store original + thumbnail (ios-engineer-4, 2026-05-17)

Pattern for History UX that needs both a list-row preview **and** a full-resolution view on the detail screen.

### Two encoders, two caps

| Field | Cap | Quality | Target size | Used by |
|---|---|---|---|---|
| `thumbnailData` | 512 px long edge | 0.7 | ~50 KB | History list row |
| `originalImageData` | 2048 px long edge | 0.85 | ~1–2 MB | Detail screen + full-screen viewer |

Keep them as separate enums (e.g. `ThumbnailGenerator`, `OriginalImageEncoder`) with constants exposed as `static let` properties and pinned by a unit test:

```swift
func test_quality_andCap_areTheDocumentedValues() {
    XCTAssertEqual(OriginalImageEncoder.maxLongEdge, 2048)
    XCTAssertEqual(OriginalImageEncoder.jpegQuality, 0.85)
}
```

### Lightweight schema migration (additive optional)

Adding `var originalImageData: Data?` to an existing `@Model` class is a SwiftData lightweight migration — no `VersionedSchema` declared, no `SchemaMigrationPlan`. Default the constructor param to `nil` so old call sites still compile:

```swift
init(
    // …existing params…
    originalImageData: Data? = nil,
    // …
) { … }
```

Verify with two tests: round-trip the new field, and confirm it defaults to `nil` when the constructor is called without it.

**Rule:** stay with lightweight migration until you need a non-additive change (rename, type-change, non-optional with no default). Promoting to a `VersionedSchema` adds significant boilerplate — defer until needed.

### One detached task, two encodes

Run both encoders inside a single `Task.detached(priority: .userInitiated)` so you only hop back to the main actor once for the ModelContext insert:

```swift
@MainActor
private func saveToHistory(image: UIImage, response: IdentifyResponse) async {
    let (thumb, original) = await Task.detached(priority: .userInitiated) {
        (
            ThumbnailGenerator.generate(from: image),
            OriginalImageEncoder.encode(from: image)
        )
    }.value
    // …encode response JSON, insert HistoryEntry on main actor…
}
```

### Three-state fallback enum

Pre-migration entries have `originalImageData == nil`. Centralise the resolution rule in a value type, not in the view body, so it's unit-testable without a SwiftUI host:

```swift
enum HistoryImageSource: Equatable {
    case original(Data)         // full-res available
    case thumbnailOnly(Data)    // pre-migration entry; show "thumbnail only" badge
    case missing                // both nil — defensive placeholder

    static func resolve(for entry: HistoryEntry) -> HistoryImageSource {
        if let d = entry.originalImageData, !d.isEmpty { return .original(d) }
        if let d = entry.thumbnailData,    !d.isEmpty { return .thumbnailOnly(d) }
        return .missing
    }
}
```

The view switches over the three cases and shows a small "Thumbnail only" label for `.thumbnailOnly`. Keep the tap-to-fullscreen gesture enabled even on thumbnail-only entries — a disabled gesture feels broken; the badge is the honest signal.

### Full-screen viewer (minimum viable)

```swift
struct FullScreenImageView: View {
    let image: UIImage
    let onDismiss: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            Image(uiImage: image).resizable().scaledToFit().padding()
            Button(action: onDismiss) {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 30))
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(.white, .black.opacity(0.5))
            }
            .padding([.top, .trailing])
        }
        .statusBarHidden(true)
    }
}
```

Present via `.fullScreenCover(item: $wrappedImage)` where `$wrappedImage` binds to a `IdentifiableImage?` wrapper. Pinch-to-zoom is an easy follow-up (host the image in a `UIScrollView`); shipping without it is fine for v1.

### Detail screen layout

Put a pinned `HistoryImageHeader` above the existing `ResultView`. Don't nest scroll views — let the header live outside the scroll container with a `.scaledToFit().frame(maxHeight: 400)` cap so it never dominates the screen.

```swift
VStack(spacing: 0) {
    HistoryImageHeader(source: imageSource) { image in
        fullScreenImage = IdentifiableImage(image: image)
    }
    ResultView(response: response) { dismiss() }
}
.fullScreenCover(item: $fullScreenImage) { wrapper in
    FullScreenImageView(image: wrapper.image) { fullScreenImage = nil }
}
```
