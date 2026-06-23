#!/bin/bash
# ============================================================
#  Push Changes — publish your edits to GitHub (bot-safe)
#  Double-click this to send your changes (scraper, workflow,
#  dashboard page, docs) up to GitHub.
#
#  Safe to use once the scraper auto-runs on GitHub: this syncs
#  with GitHub first, never overwrites the data the bot collects,
#  and retries if the bot pushes at the same moment. Does NOT
#  run the scraper.
# ============================================================

cd "$(dirname "$0")" || exit 1

LIVE_URL="https://owenamurray04.github.io/cinemark-dbox/dashboard/"

echo ""
echo "==================================================="
echo "   PUSH YOUR CHANGES TO GITHUB  (Cinemark D-BOX)"
echo "==================================================="
echo ""

# Clear stale git locks left by an interrupted/crashed git op (only if no git is
# running). Includes HEAD.lock, which a sandboxed editor can leave behind.
if ! pgrep -x git >/dev/null 2>&1; then
  for lk in .git/index.lock .git/HEAD.lock .git/config.lock .git/refs/heads/*.lock; do
    [ -e "$lk" ] && { echo "Clearing a leftover git lock ($lk)..."; rm -f "$lk"; }
  done
fi

# 1. Don't fight the automation: the GitHub bot owns these files. Throw away any
#    local changes to them (and any stray untracked copies) so they can't cause
#    a merge conflict.
git checkout -- dashboard/cinemark_data.json schedule_cinemark dbox_theatres_cache.json dashboard/cinemark_br_data.json schedule_cinemark_br dbox_theatres_cache_br.json >/dev/null 2>&1
git clean -fd schedule_cinemark schedule_cinemark_br >/dev/null 2>&1

# 2. Get up to date with GitHub first (pulls in the bot's commits).
echo "Syncing with GitHub..."
if ! git pull --rebase --autostash; then
  echo ""
  echo "X  Couldn't sync with GitHub. Read the message above, fix it, then"
  echo "   run this again. (Often it's just an internet/login hiccup.)"
  echo ""
  read -p "Press RETURN to close..." _
  exit 1
fi

# 3. Stage only YOUR changes — never the bot-owned data files.
git rm --cached --quiet .DS_Store >/dev/null 2>&1
git rm -r --cached --quiet __pycache__ >/dev/null 2>&1
rm -rf __pycache__ >/dev/null 2>&1
git add -A
git reset -q -- dashboard/cinemark_data.json schedule_cinemark dbox_theatres_cache.json dashboard/cinemark_br_data.json schedule_cinemark_br dbox_theatres_cache_br.json

# 4. Anything of yours to publish?
if git diff --cached --quiet; then
  echo ""
  echo "You're already in sync with GitHub — nothing of yours to push."
  echo ""
  read -p "Press RETURN to close..." _
  exit 0
fi

echo ""
echo "These changes will be published:"
echo "---------------------------------------------------"
git status --short
echo "---------------------------------------------------"
echo ""

read -p "Short note for this update [press RETURN for \"Update code\"]: " MSG
MSG="${MSG:-Update code}"
git commit -m "$MSG" >/dev/null

# 5. Push — retry if the bot commits at the same instant.
echo ""
echo "Publishing..."
PUSHED=0
for i in 1 2 3 4 5; do
  if git push; then
    PUSHED=1
    break
  fi
  echo "  Re-syncing and retrying ($i)..."
  git checkout -- dashboard/cinemark_data.json schedule_cinemark dbox_theatres_cache.json dashboard/cinemark_br_data.json schedule_cinemark_br dbox_theatres_cache_br.json >/dev/null 2>&1
  git pull --rebase --autostash >/dev/null 2>&1
  sleep $((RANDOM % 4 + 2))
done

echo ""
if [ "$PUSHED" -eq 1 ]; then
  echo "==================================================="
  echo "   PUBLISHED"
  echo "   Live page (gives ~1 min to refresh):"
  echo "   $LIVE_URL"
  echo "==================================================="
else
  echo "X  Push kept failing. Check your internet / GitHub login and try again."
fi

echo ""
read -p "Press RETURN to close this window..." _
