# Skill: xcconfig Include Order

## Rule
In Xcode `.xcconfig` files, **`#include` or `#include?` directives must come AFTER the defaults** if the included file is meant to override them.

In xcconfig merge semantics, **later assignments win**. An assignment that appears after an `#include` will override any matching keys from the included file.

## Pattern
```xcconfig
// ❌ WRONG — included values will be overridden by defaults below:
#include? "Config.local.xcconfig"
API_BASE_URL = http://localhost:8000
API_KEY      = dev-replace-me

// ✓ RIGHT — included values override defaults because include is last:
API_BASE_URL = http://localhost:8000
API_KEY      = dev-replace-me
#include? "Config.local.xcconfig"
```

## Application
Used in `apps/ios/ArtGuide/Config/Config.xcconfig` to ensure `Config.local.xcconfig` (containing production API credentials) overrides the defaults (localhost + placeholder key).

See commit: fix(ios): move xcconfig #include to end so local overrides win
