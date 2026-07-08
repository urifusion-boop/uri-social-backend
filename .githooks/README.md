# Git Hooks

This directory contains git hooks that help prevent common issues.

## Setup

After cloning the repository, run:

```bash
git config core.hooksPath .githooks
```

Or run the install script:

```bash
./scripts/install-hooks.sh
```

## Hooks

### pre-push

Prevents pushing when remote has new commits you don't have locally.

This prevents accidentally overwriting other developers' changes.

**What it does:**
- Fetches latest changes from remote
- Checks if your branch is behind remote
- Blocks push if you need to pull first
- Shows clear error message with instructions

**Example error:**
```
❌ ERROR: Remote has new commits that you don't have locally!
❌ Please run: git pull origin develop --rebase
❌ Then try pushing again
```
