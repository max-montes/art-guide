# Session Log — XcodeGen Setup (2026-05-10)

## Summary

iOS app project generation now deterministically driven by `apps/ios/project.yml` (XcodeGen spec). The `.xcodeproj` is a build artifact, generated once by `apps/ios/setup.sh` and then gitignored. No manual Xcode UI work required ever again.

## Key Context

**Before:** Project owner had to create a new SwiftUI project in Xcode, save it, move `.xcodeproj` to the repo, delete auto-generated files, re-import on-disk sources, disable GENERATE_INFOPLIST_FILE, wire Config.xcconfig for both configurations—error-prone and not repeatable.

**Now:** One command: `brew install xcodegen && cd apps/ios && ./setup.sh && open ArtGuide.xcodeproj`

**Why:** XcodeGen was already identified in the swift-ui-xcode-gen decision record (not merged until now). The critical setting `GENERATE_INFOPLIST_FILE = NO` + `INFOPLIST_FILE = ArtGuide/Info.plist` lives in YAML and survives re-generation. Adding future files is automatic.

## Files Changed

- `apps/ios/project.yml` ← new
- `apps/ios/setup.sh` ← new
- `apps/ios/README.md` ← rewritten
- `.gitignore` ← updated for `.xcodeproj/` and user state

## Decision

**D-017** merged to `decisions.md` (owner: ios-engineer, status: Active).
