#!/usr/bin/env sh
set -eu

PACKAGE_SPEC="${SHERLOCK_PACKAGE_SPEC:-git+https://github.com/TinyBlackHole/agent-sherlock.git}"

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
