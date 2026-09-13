"""Content-bound review identity shared by CI and the installed merge gate."""
import hashlib
import json
import re
import subprocess

SHA = re.compile(r'\b[0-9a-fA-F]{40}\b')
DELTA = re.compile(r'delta-from:\s*([0-9a-fA-F]{40})\b', re.I)
EXPLICIT = re.compile(r'(?im)^\s*reviewed-sha:\s*([0-9a-fA-F]{40})\b')


def review_target(review):
    """The single commit a review binds to, or None when the body is ambiguous.

    Precedence: an explicit ``reviewed-sha:`` line, then GitHub's authoritative
    ``commit_id`` metadata, then an unambiguous single mention in prose. Bodies
    that merely quote extra 40-hex tokens (prior heads, patch-ids, digests) are
    resolved by metadata instead of being discarded.
    """
    body = review.get('body') or ''
    explicit = EXPLICIT.search(body)
    if explicit:
        return explicit.group(1).lower()
    metadata = review.get('commit_id') or (review.get('commit') or {}).get('oid') or review.get('commitRefOid')
    if metadata:
        return metadata.lower()
    named = set(s.lower() for s in SHA.findall(DELTA.sub('', EXPLICIT.sub('', body))))
    return next(iter(named)) if len(named) == 1 else None


def target_shas(reviews, head=None):
    """Commits the engine may have to diff, so callers fetch those and only those.

    An arbitrary 40-hex token in review prose is not a commit — a quoted
    patch-id fetched as one aborts the whole check — so only declared targets
    and delta sources are returned.
    """
    wanted = {head.lower()} if head else set()
    for review in reviews:
        wanted.update(m.group(1).lower() for m in DELTA.finditer(review.get('body') or ''))
        target = review_target(review)
        if target:
            wanted.add(target)
    return wanted


def json_pages(text):
    decoder = json.JSONDecoder()
    pages = []
    while text.strip():
        text = text.lstrip()
        page, end = decoder.raw_decode(text)
        pages.append(page)
        text = text[end:]
    return pages


def git(*args, cwd=None, input=None):
    return subprocess.check_output(['git', *args], cwd=cwd, input=input).decode().strip()


def is_ancestor(source, sha, cwd=None):
    return subprocess.run(['git', 'merge-base', '--is-ancestor', source, sha], cwd=cwd,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def content_identity(sha, base='origin/staging', cwd=None):
    if not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise ValueError('Review/head must name a full commit SHA')
    git('cat-file', '-e', sha + '^{commit}', cwd=cwd)
    ancestor = git('merge-base', base, sha, cwd=cwd)
    diff = subprocess.check_output(['git', 'diff', '--binary', ancestor + '..' + sha], cwd=cwd)
    result = git('patch-id', '--stable', cwd=cwd, input=diff)
    normalized = b''.join(line for line in diff.splitlines(keepends=True)
                          if not line.startswith((b'@@ ', b'index ')))
    return (result.split()[0] if result else 'empty', hashlib.sha256(normalized).hexdigest())


def evaluate(reviews, head, author, base='origin/staging', cwd=None, staging=False):
    head_id, head_digest = content_identity(head, base, cwd)
    covered = {}
    latest = None
    for review in sorted(reviews, key=lambda r: (r.get('submitted_at') or r.get('submittedAt') or '', r.get('id') or 0)):
        state = review.get('state', '').upper()
        actor = (review.get('user') or review.get('author') or {}).get('login', '')
        body = review.get('body') or ''
        if not actor or (actor.lower() == author.lower() and not staging):
            continue
        verdict_line = next((line.strip().strip('#* ') for line in body.splitlines() if line.strip()), '')
        verdict = re.match(r'(?:verdict:\s*)?(REQUEST[-_]CHANGES|CLOSE|APPROVED?|SAFE_AS_IS|LGTM)\b', verdict_line, re.I)
        rejected = state == 'CHANGES_REQUESTED' or (verdict and verdict.group(1).upper() in ('REQUEST-CHANGES', 'REQUEST_CHANGES', 'CLOSE'))
        if rejected:
            latest = {'reviewed_sha': review.get('commit_id'), 'reviewed_patch_id': None,
                      'head_patch_id': head_id, 'reviewer': actor, 'state': state,
                      'valid_chain': False, 'rejected': True}
            continue
        if state not in ('APPROVED', 'COMMENTED', 'COMMENT'):
            continue
        if not staging and state != 'APPROVED':
            continue
        if state != 'APPROVED' and not verdict:
            continue
        sha = review_target(review)
        if not sha:
            continue
        identity = content_identity(sha, base, cwd)
        # Every declared source must itself be a valid approval carried in this run and
        # an ancestor of the reviewed commit, so a body naming several sources has
        # defined semantics instead of silently binding to the first one.
        sources = [m.group(1).lower() for m in DELTA.finditer(body)]
        valid = all(source in covered and is_ancestor(source, sha, cwd) for source in sources)
        latest = {'reviewed_sha': sha, 'reviewed_patch_id': identity[0], 'reviewed_digest': identity[1],
                  'head_patch_id': head_id, 'head_digest': head_digest,
                  'reviewer': actor, 'state': state, 'valid_chain': valid}
        if valid:
            covered[sha] = identity
    if not latest:
        return {'passed': False, 'reviewed_sha': None, 'reviewed_patch_id': None, 'head_patch_id': head_id, 'reason': 'no qualifying independent approval'}
    latest['head_digest'] = head_digest
    latest['passed'] = latest['valid_chain'] and latest['reviewed_patch_id'] == head_id and latest.get('reviewed_digest') == head_digest
    latest['reason'] = 'latest independent verdict requests changes' if latest.get('rejected') else (('review matches current content' if latest['reviewed_sha'] == head else 'sync-only push, review still valid') if latest['passed'] else f"diff changed since {latest['reviewed_sha']}, delta review required")
    return latest
