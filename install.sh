#!/usr/bin/env sh
set -eu

# A versioned release tag is the default. Tracking the mutable default branch
# means two installs an hour apart can ship different code, so overriding this
# is a deliberate developer action rather than the norm. Release tags must be
# protected against updates in the repository settings.
SHERLOCK_VERSION="${SHERLOCK_VERSION:-v0.1.0}"
REPOSITORY="${SHERLOCK_REPOSITORY:-https://github.com/TinyBlackHole/agent-sherlock.git}"
PACKAGE_SPEC="${SHERLOCK_PACKAGE_SPEC:-git+${REPOSITORY}@${SHERLOCK_VERSION}}"

echo "Installing Agent Sherlock from ${PACKAGE_SPEC}"

if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$PACKAGE_SPEC"
elif command -v python3 >/dev/null 2>&1; then
  python3 -m pip install --user --upgrade "$PACKAGE_SPEC"
  case ":$PATH:" in
    *:"$HOME/.local/bin":*) ;;
    *)
      echo "Installed with pip. Make sure $HOME/.local/bin is in your PATH." >&2
      ;;
  esac
else
  echo "python3 is required to install Agent Sherlock." >&2
  exit 1
fi

echo "Agent Sherlock installed."
echo "Run: sherlock -v"
