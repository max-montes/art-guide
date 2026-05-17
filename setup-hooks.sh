#!/usr/bin/env bash
# setup-hooks.sh — one-time per-clone setup for git hooks.
#
# Points git's hooksPath at the committed .githooks/ directory so the
# XcodeGen pre-commit hook runs automatically when Swift files are staged.
#
# Usage:
#   ./setup-hooks.sh
set -euo pipefail

git config core.hooksPath .githooks
echo "✅ Git hooks installed. .githooks/pre-commit will auto-regen ArtGuide.xcodeproj when Swift files are staged."
