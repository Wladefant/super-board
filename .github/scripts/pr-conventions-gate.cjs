#!/usr/bin/env node
/**
 * PR Conventions Gate
 *
 * Consolidated single-job gate enforcing PR conventions:
 * 1. Required Labels: At least one canonical `kind:*` and one `area:*`
 * 2. Linked Issue: Closing keyword + issue reference in PR body
 * 3. Milestone Assigned: Non-null milestone from repository roadmap
 * 4. Scope & Size Guard: additions + deletions <= 500 lines, unless exempt
 *
 * Reference: Superboard Issue #111 & #113
 */

const fs = require('fs');
const { execSync } = require('child_process');

function fetchSubIssues(repo, issueNumber) {
  try {
    const stdout = execSync(`gh api repos/${repo}/issues/${issueNumber}/sub_issues`, {
      encoding: 'utf8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 10000,
    });
    const parsed = JSON.parse(stdout);
    if (Array.isArray(parsed)) {
      return parsed.filter(item => item && item.number);
    }
  } catch (e) {
    // If gh api fails (e.g. no sub-issues, endpoint not available, or network error)
  }
  return [];
}

const ALLOWED_KINDS = [
  'kind:bug',
  'kind:feature',
  'kind:task',
  'kind:research',
  'kind:docs',
  'kind:governance',
  'kind:incident',
];

const ALLOWED_AREAS = [
  'area:workflow',
  'area:harness',
  'area:bridge',
  'area:sync',
  'area:ui',
  'area:infra',
  'area:security',
];

const MAX_DIFF_LINES = 500;
const SIZE_EXEMPT_LABELS = ['kind:docs', 'size:exempt'];

function parseEventPayload() {
  const eventPath = process.argv[2] || process.env.GITHUB_EVENT_PATH;
  if (!eventPath) {
    throw new Error('No event payload path provided. Set GITHUB_EVENT_PATH or pass payload file as argument.');
  }
  if (!fs.existsSync(eventPath)) {
    throw new Error(`Event payload file not found: ${eventPath}`);
  }
  const raw = fs.readFileSync(eventPath, 'utf8');
  return JSON.parse(raw);
}

function evaluatePR(pr, options = {}) {
  const failures = [];
  const passes = [];

  const labels = (pr.labels || []).map(l => (typeof l === 'string' ? l : (l && l.name) || ''));
  const body = pr.body || '';
  const milestone = pr.milestone;
  const additions = typeof pr.additions === 'number' ? pr.additions : 0;
  const deletions = typeof pr.deletions === 'number' ? pr.deletions : 0;
  const totalDiff = additions + deletions;
  const prNumber = pr.number || 'UNKNOWN';

  // 1a. Required Labels: kind:*
  const matchedKinds = labels.filter(l => ALLOWED_KINDS.includes(l));
  if (matchedKinds.length === 0) {
    failures.push({
      check: 'Required Labels: kind:*',
      message: `Missing required 'kind:*' label. Found: [${labels.join(', ') || 'none'}].`,
      remedy: `Add at least one categorical kind label (${ALLOWED_KINDS.join(', ')}).\nExample: gh pr edit ${prNumber} --add-label "kind:task"`,
    });
  } else {
    passes.push(`Kind label: ${matchedKinds.join(', ')}`);
  }

  // 1b. Required Labels: area:*
  const matchedAreas = labels.filter(l => ALLOWED_AREAS.includes(l) || /^area:[a-z0-9_-]+$/i.test(l));
  if (matchedAreas.length === 0) {
    failures.push({
      check: 'Required Labels: area:*',
      message: `Missing required 'area:*' label. Found: [${labels.join(', ') || 'none'}].`,
      remedy: `Add at least one subsystem area label (${ALLOWED_AREAS.join(', ')}).\nExample: gh pr edit ${prNumber} --add-label "area:workflow"`,
    });
  } else {
    passes.push(`Area label: ${matchedAreas.join(', ')}`);
  }

  // 2. Linked Issue
  const issueRefRegex = /(?:fixes|closes|resolves|refs|part of)\s+(?:https:\/\/github\.com\/([^\s/]+\/[^\s/]+)\/issues\/|#|([a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+)#)(\d+)/i;
  const issueMatch = body.match(issueRefRegex);
  if (!issueMatch) {
    failures.push({
      check: 'Linked Issue',
      message: 'PR description does not contain a valid issue reference (Fixes, Closes, Resolves, Refs, Part of).',
      remedy: `Add an issue reference under "## Linked Issue" using format: Fixes #123 (leaf issue) or Refs #123 / Part of #123 (parent issue).\nExample: gh pr edit ${prNumber} --body "$(cat <<'EOF'\n## Linked Issue\nFixes #123\n\n$(gh pr view ${prNumber} --json body -q .body)\nEOF\n)"`,
    });
  } else {
    passes.push(`Linked issue: ${issueMatch[0]}`);
  }

  // 2b. Parent-Close Guard: Reject closing keywords (Fixes, Closes, Resolves) on parent issues with open sub-issues
  const closingKeywordRegex = /\b(fixes|closes|resolves)\s+(?:https:\/\/github\.com\/([^\s/]+\/[^\s/]+)\/issues\/|#|([a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+)#)(\d+)/gi;
  let closingMatch;
  const defaultRepo = (pr.base && pr.base.repo && pr.base.repo.full_name) || process.env.GITHUB_REPOSITORY || 'Wladefant/super-board';
  const getSubIssues = (options && options.subIssuesFetcher) || ((r, n) => fetchSubIssues(r, n));

  while ((closingMatch = closingKeywordRegex.exec(body)) !== null) {
    const kw = closingMatch[1];
    const targetRepo = closingMatch[2] || closingMatch[3] || defaultRepo;
    const targetIssueNum = parseInt(closingMatch[4], 10);
    const subIssues = getSubIssues(targetRepo, targetIssueNum) || [];
    const openSubIssues = subIssues.filter(s => String(s.state || 'open').toLowerCase() === 'open');
    if (openSubIssues.length > 0) {
      const openDesc = openSubIssues.map(s => `#${s.number}: ${s.title || ''} (${s.state || 'open'})`).join('; ');
      failures.push({
        check: 'Parent Issue Closing Keyword Guard',
        message: `PR uses closing keyword '${kw}' targeting parent issue #${targetIssueNum}, which has ${openSubIssues.length} open sub-issue(s): ${openDesc}.`,
        remedy: `Parent issues cannot be closed while sub-issues remain open. Change closing keyword '${kw} #${targetIssueNum}' to a non-closing reference ('Refs #${targetIssueNum}' or 'Part of #${targetIssueNum}').`,
      });
    }
  }

  // 3. Milestone
  if (!milestone || (!milestone.title && !milestone.number)) {
    failures.push({
      check: 'Milestone Assigned',
      message: 'PR has no milestone assigned.',
      remedy: `Every PR must belong to an active capability milestone / integration wave.\nExample: gh pr edit ${prNumber} --milestone "GitHub System Integration"`,
    });
  } else {
    passes.push(`Milestone: ${milestone.title || `#${milestone.number}`}`);
  }

  // 4. Scope / Size Guard
  const isSizeExempt = labels.some(l => SIZE_EXEMPT_LABELS.includes(l));
  if (!isSizeExempt && totalDiff > MAX_DIFF_LINES) {
    failures.push({
      check: 'Scope & Size Guard',
      message: `PR diff scope exceeds single-deliverable threshold (+${additions}/-${deletions} = ${totalDiff} lines > ${MAX_DIFF_LINES} limit).`,
      remedy: `Split this pull request into smaller, atomic single-deliverable PRs (stacked via merge commits).\nIf this PR is pure documentation or an authorized migration, apply label 'kind:docs' or 'size:exempt'.`,
    });
  } else {
    passes.push(`Diff scope: ${totalDiff} lines (+${additions}/-${deletions})${isSizeExempt ? ' [size:exempt]' : ''}`);
  }

  return { failures, passes, totalDiff, prNumber };
}

function run() {
  let payload;
  try {
    payload = parseEventPayload();
  } catch (err) {
    console.error(`[ERROR] Failed to load event payload: ${err.message}`);
    process.exit(2);
  }

  const pr = payload.pull_request;
  if (!pr) {
    console.log('[SKIP] Event is not a pull_request event. Skipping conventions gate.');
    process.exit(0);
  }

  const { failures, passes, prNumber } = evaluatePR(pr);

  console.log('============================================================');
  console.log(`          PR CONVENTIONS GATE: PR #${prNumber}`);
  console.log('============================================================');

  for (const pass of passes) {
    console.log(`[PASS] ${pass}`);
  }

  if (failures.length > 0) {
    console.log('\n------------------------------------------------------------');
    console.log(`  CONVENTIONS CHECK FAILED (${failures.length} requirement(s) missing)`);
    console.log('------------------------------------------------------------');

    failures.forEach((fail, idx) => {
      console.log(`\n${idx + 1}. [FAIL] ${fail.check}`);
      console.log(`   Issue:  ${fail.message}`);
      console.log(`   Fix:    ${fail.remedy.split('\n').join('\n           ')}`);

      if (process.env.GITHUB_ACTIONS === 'true') {
        const singleLineRemedy = fail.remedy.replace(/\r?\n/g, ' ');
        console.log(`::error title=${fail.check}::${fail.message} Fix: ${singleLineRemedy}`);
      }
    });

    console.log('\n============================================================');
    console.log('Please resolve the failing items above to satisfy the gate.');
    console.log('============================================================');
    process.exit(1);
  }

  console.log('\n============================================================');
  console.log('  SUCCESS: ALL PR CONVENTIONS SATISFIED');
  console.log('============================================================');
  process.exit(0);
}

if (require.main === module) {
  run();
}

module.exports = { evaluatePR, fetchSubIssues };
