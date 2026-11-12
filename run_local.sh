#!/bin/bash
# Full check from this computer, then publish the results to the shared page.
# Needed because the UGC portal and several state sites only answer from India.
set -e
cd "$(dirname "$0")"
git pull -q --rebase origin main
python3 tracker.py
git add data docs
git diff --cached --quiet || git commit -q -m "Update openings (full check)"
git push -q origin main
echo "Published."
