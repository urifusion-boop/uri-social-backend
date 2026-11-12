#!/bin/bash

# Install git hooks from .githooks directory
echo "🔧 Installing git hooks..."

# Configure git to use .githooks directory
git config core.hooksPath .githooks

if [ $? -eq 0 ]; then
    echo "✅ Git hooks installed successfully!"
    echo "📁 Hooks location: .githooks/"
    echo ""
    echo "Active hooks:"
    ls -1 .githooks/ | grep -v "README.md" | sed 's/^/  - /'
else
    echo "❌ Failed to install git hooks"
    exit 1
fi
