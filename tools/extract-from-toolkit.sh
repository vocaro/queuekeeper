#!/bin/sh
# Extract Queuekeeper's code, with its history, from vocaro/vocaro-engineering.
#
#   tools/extract-from-toolkit.sh DESTINATION
#
# DESTINATION must not exist and must be outside every checkout. The script clones the
# toolkit there afresh and rewrites that clone only, never a working checkout:
#   - it keeps the paths listed in tools/extract-paths.txt, with every commit that
#     touched them, and drops the rest;
#   - it rewrites a bare `#NNN` in a commit message to `vocaro/vocaro-engineering#NNN`,
#     so that it does not point at one of Queuekeeper's own issues (decision L of
#     vocaro/vocaro-engineering#403).
#
# The first run seeded this repository. Until cutover the toolkit keeps changing these
# files, so the same extraction is run again and its result merged:
#
#   tools/extract-from-toolkit.sh /some/scratch/extract
#   git fetch /some/scratch/extract main
#   git merge FETCH_HEAD
#
# The rewrite is deterministic: the same toolkit history and the same path list give
# the same commit IDs, so a later run's history contains an earlier run's. Changing
# tools/extract-paths.txt changes the IDs of the commits it affects; the first merge
# after such a change then needs `--allow-unrelated-histories` if no commit is shared.
#
# Needs git filter-repo (`brew install git-filter-repo`) and access to the toolkit.
set -eu

SOURCE=${QUEUEKEEPER_EXTRACT_SOURCE:-https://github.com/vocaro/vocaro-engineering.git}
HERE=$(cd "$(dirname "$0")" && pwd)
PATHS=$HERE/extract-paths.txt

[ $# -eq 1 ] || { echo "usage: $0 DESTINATION" >&2; exit 2; }
DESTINATION=$1
[ ! -e "$DESTINATION" ] || { echo "$DESTINATION exists; name a new folder" >&2; exit 2; }
git filter-repo --version >/dev/null 2>&1 || {
    echo "git filter-repo is missing: brew install git-filter-repo" >&2; exit 2; }

git clone --quiet --no-local --single-branch --branch main "$SOURCE" "$DESTINATION"
cd "$DESTINATION"
echo "toolkit main: $(git rev-parse HEAD), $(git rev-list --count HEAD) commits"

git filter-repo \
    --paths-from-file "$PATHS" \
    --message-callback '
import re
return re.sub(rb"(?<![\w/&#])#(\d+)\b", rb"vocaro/vocaro-engineering#\1", message)
'

echo "extracted: $(git rev-parse HEAD), $(git rev-list --count HEAD) commits, $(git ls-files | wc -l | tr -d " ") files"
