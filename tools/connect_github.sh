#!/usr/bin/env bash
# Switch this AureoClean folder to update from GitHub (run once).
#   ./tools/connect_github.sh https://github.com/OWNER/aureoclean.git
# After this, the "Update AureoClean" icon downloads changes from GitHub
# instead of needing a zip in Downloads. Settings, accounts, .venv and logs
# are not touched.
set -euo pipefail
URL="${1:-}"
[ -n "$URL" ] || { echo "Usage: $0 https://github.com/OWNER/REPO.git"; exit 1; }
cd "$(dirname "$0")/.."
command -v git >/dev/null || sudo apt-get install -y git
[ -d .git ] || git init -q
git remote remove origin 2>/dev/null || true
git remote add origin "$URL"
echo "Checking GitHub…"
git fetch -q origin || { echo "Couldn't reach $URL (is the address right? is the repo private?)"; exit 1; }
BRANCH="$(git remote show origin | sed -n 's/.*HEAD branch: //p')"
git checkout -q -B "${BRANCH:-main}" "origin/${BRANCH:-main}" -f
git branch -q --set-upstream-to="origin/${BRANCH:-main}"
chmod +x run_local.sh setup_pi.sh tools/*.sh
echo "Connected. From now on just double-click \"Update AureoClean\"."
