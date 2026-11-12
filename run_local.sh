#!/bin/bash
# Full check from this computer, then publish the results to the shared page.
# Needed because the UGC portal and several state sites only answer from India.
# Requires permission to push to the GitHub repository.
set -e
cd "$(dirname "$0")"
echo "--- $(date '+%Y-%m-%d %H:%M') full check"

git checkout -q -- data docs 2>/dev/null || true   # drop leftovers from an interrupted run
git fetch -q origin main
if ! git rebase -q origin/main; then
  git rebase --abort 2>/dev/null || true
  echo "This copy has changes that clash with GitHub. Nothing was published."
  exit 1
fi

python3 tracker.py
git add data docs
if ! git diff --cached --quiet; then
  git -c user.name="job-tracker" -c user.email="job-tracker@users.noreply.github.com" \
    commit -q -m "Update openings (full check)"
fi
git push -q origin main
echo "Published."
