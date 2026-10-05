#!/usr/bin/env bash
# week-boards-inventory.sh — one batched, guard-gated GraphQL query for every
# GitHub Project (v2) the operator can see: the user, the viewer's orgs and any
# extra owner passed in WEEK_EXTRA_ORGS (space separated).
#
# Output (stdout): JSON array, one object per board:
#   { owner, number, title, closed, items, dateFields, repos: { "<owner/repo>": n } }
# `repos` counts the Repository of the 100 most recent items (all items when a
# board holds 100 or fewer). Exit 75 = guard refused, nothing was queried.
#
# Usage: scripts/week-boards-inventory.sh [user-login]
set -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/super-board-gh-guard.sh
. "$here/super-board-gh-guard.sh"

user="${1:-Wladefant}"
sb_gh_guard_begin_cycle || true
sb_gh_guard_check 103 || exit 75

frag='nodes{number title closed url items(last:100){totalCount nodes{content{__typename ... on Issue{repository{nameWithOwner}} ... on PullRequest{repository{nameWithOwner}}}}} fields(first:30){nodes{... on ProjectV2FieldCommon{name dataType}}}}'
query="query(\$u:String!){user(login:\$u){projectsV2(first:30){$frag}} viewer{organizations(first:20){nodes{login projectsV2(first:30){$frag}}}}"
for org in ${WEEK_EXTRA_ORGS:-}; do
  a="o_${org//[^A-Za-z0-9]/_}"
  query+=" $a:organization(login:\"$org\"){login projectsV2(first:30){$frag}}"
done
query+="}"

raw=$(timeout 120 "${SUPERBOARD_GH:-gh}" api graphql -f query="$query" -F u="$user") || { echo "[inventory] graphql failed" >&2; exit 1; }

printf '%s' "$raw" | jq --arg user "$user" '
  def board($owner): .nodes[]? | {
    owner: $owner, number, title, closed, url,
    items: .items.totalCount,
    dateFields: [.fields.nodes[]? | select(.dataType == "DATE" or .dataType == "ITERATION") | .name],
    repos: ([.items.nodes[]?.content.repository.nameWithOwner? | select(. != null)] | group_by(.) | map({key: .[0], value: length}) | from_entries)
  };
  [ (.data.user.projectsV2 | board($user)),
    (.data.viewer.organizations.nodes[]? | .login as $o | .projectsV2 | board($o)),
    (.data | to_entries[] | select(.key | startswith("o_")) | .value | select(. != null) | .login as $o | .projectsV2 | board($o)) ]
  | unique_by([.owner, .number]) | sort_by([.owner, .number])'
rc=$?
sb_gh_guard_summary
exit $rc
