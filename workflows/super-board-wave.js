export const meta = {
  name: 'super-board-wave',
  description: 'Drain one super-board wave: classify, then build → qa → review per card (lifecycles per run.md)',
  whenToUse: 'Launched by the super-board run-workflow backend with args from super-board-wave-plan.sh. Not for direct ad-hoc use.',
  phases: [
    { title: 'Classify', detail: 'haiku router: kind + complexity per Ready card' },
    { title: 'Build', detail: 'Builder lifecycle (run.md): worktree, branch, draft PR' },
    { title: 'QA', detail: 'Tester lifecycle (run.md): test plan, evidence, screenshots' },
    { title: 'Review', detail: 'Reviewer lifecycle (run.md): gates, rerun tests, stop at the human handoff' },
  ],
}

// args = {
//   configPath: '.claude/super-board/configs/<slug>.json',
//   variant: 'full' | 'qa-only',
//   cards: [...],                           // `.cards` from super-board-wave-plan.sh
//   humanApprovesMerge: boolean (optional, default true — the runtime never merges),
//   tier: 'low' | 'medium' | 'high' (optional, default 'medium'),  // run model ladder
// }
// The harness can deliver `args` as a JSON-encoded string (the tool param is
// untyped) — normalize before validating.
const input = (() => {
  if (typeof args !== 'string') return args
  try { return JSON.parse(args) } catch { return args }
})()
if (!input || !Array.isArray(input.cards) || !input.configPath || !input.variant) {
  throw new Error('super-board-wave needs args {configPath, variant, cards:[…]} from super-board-wave-plan.sh')
}
if (input.tier && !['low', 'medium', 'high'].includes(input.tier)) {
  throw new Error(`super-board-wave: unknown tier "${input.tier}" — use low | medium | high`)
}

// Eligibility is NOT re-derived here. `super-board-wave-plan.sh` already ran the
// shared runtime (super_board_runtime.eligibility), which is the only place the
// rules live: issue cards only, never `design`/`history`, status EXACTLY `Ready`,
// unclaimed, OPEN, unambiguous branch route, activation permitting. This workflow
// consumes that decision verbatim and refuses anything that does not carry it —
// re-deriving the rules here is exactly how the three paths drifted apart before.
for (const card of input.cards) {
  if (!card || typeof card.number !== 'number') {
    throw new Error('super-board-wave: every card needs a numeric issue number from the planner')
  }
  if (card.status !== 'Ready') {
    throw new Error(
      `super-board-wave: card #${card.number} arrived with status "${card.status}" — only ` +
      `status "Ready" is dispatchable. Re-run super-board-wave-plan.sh; do not hand-assemble cards.`
    )
  }
  // Activation travels with the card. A wave cannot be launched for a board that
  // is off, and nothing in this workflow can turn a board on.
  if (!['proof-only', 'active'].includes(card.activationMode || card.activation_mode)) {
    throw new Error(
      `super-board-wave: card #${card.number} carries activation mode ` +
      `"${card.activationMode || card.activation_mode}" — dispatch is not activated for this board.`
    )
  }
}

const activationMode = input.cards.length
  ? (input.cards[0].activationMode || input.cards[0].activation_mode)
  : 'off'
if (activationMode === 'proof-only' && input.cards.length > 1) {
  throw new Error(
    `super-board-wave: proof-only activation permits exactly one card; the planner handed over ` +
    `${input.cards.length}. Refusing the wave.`
  )
}

const CLASSIFY_SCHEMA = {
  type: 'object',
  properties: {
    kind: { type: 'string', enum: ['feature', 'bug', 'docs', 'chore'] },
    complexity: { type: 'string', enum: ['low', 'medium', 'high'] },
  },
  required: ['kind', 'complexity'],
}

const STAGE_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['advanced', 'bounced', 'blocked', 'human-gate', 'failed'] },
    column: { type: 'string' },
    detail: { type: 'string' },
    prUrl: { type: 'string' },
    branch: { type: 'string' },
    // Exact-SHA QA evidence travels with every stage result so the merge
    // handoff can be validated without re-deriving anything downstream.
    testedSha: { type: 'string' },
    currentHeadSha: { type: 'string' },
    checkConclusion: { type: 'string' },
  },
  required: ['status', 'column', 'detail'],
}

// The read-only merge handoff. Mirrors
// `super_board_runtime.qa.validate_merge_handoff`: the live head must still be
// the tested commit AND the SHA-bound required check on that commit must have
// concluded success. It performs no writes and it never merges — the runtime
// has no merge path. It only decides whether a card may be REPORTED as ready
// for the human's rebase merge.
const QA_CHECK_CONTEXT = 'superboard/exact-sha-qa'
const validateMergeHandoff = (result) => {
  const tested = (result && result.testedSha) || null
  const current = (result && result.currentHeadSha) || null
  const conclusion = (result && result.checkConclusion) || null
  if (!tested) return { mergeReady: false, reasonCode: 'qa-evidence-missing', tested, current }
  if (!current || current !== tested) {
    return { mergeReady: false, reasonCode: 'head-moved', tested, current }
  }
  if (!conclusion || !String(conclusion).trim()) {
    return { mergeReady: false, reasonCode: 'check-missing', tested, current }
  }
  if (String(conclusion).trim().toLowerCase() !== 'success') {
    return { mergeReady: false, reasonCode: 'check-not-success', tested, current }
  }
  return { mergeReady: true, reasonCode: null, tested, current }
}

const LANE = {
  build:  { skill: 'super-build',  section: 'Builder',  phase: 'Build' },
  qa:     { skill: 'super-qa',     section: 'Tester',   phase: 'QA' },
  review: { skill: 'super-review', section: 'Reviewer', phase: 'Review' },
}

// Review serialization guard, execution side: Reviewer agents contend for the
// same base branch and the same PR-ready handoff, so Review lanes run one at a
// time unless a human is explicitly the merge gate. The runtime itself never
// merges. Promise-chain mutex; the catch keeps one failed review from poisoning
// the chain.
let reviewLock = Promise.resolve()
const withReviewLock = (fn) => {
  const run = reviewLock.then(fn)
  reviewLock = run.then(() => undefined, () => undefined)
  return run
}

const lanePrompt = (lane, card) => [
  `Run ${LANE[lane].skill} on issue #${card.number} ("${card.title}") for a super-board workflow wave.`,
  `Read .claude/skills/super-board/references/run.md → "${LANE[lane].section}" lifecycle and follow it EXACTLY:`,
  `create your own worktree under .worktrees/, work on the issue branch, post the required PR/issue comments,`,
  `move the project card yourself, clean up the worktree on exit. Config: ${input.configPath}.`,
  ``,
  `Report your exit via structured output:`,
  `- status=advanced  → card moved forward (Building→QA, QA→Review, Review→Done/merged)`,
  `- status=bounced   → card moved backward (QA fail → Ready, Reviewer bounce → Ready/QA)`,
  `- status=blocked or human-gate → you wrote the Block template and moved the card to Blocked`,
  `- status=failed    → you could not complete the lifecycle (say why in detail)`,
  `column = the column the card is in when you exit. detail = one line. Include prUrl/branch when they exist.`,
].join('\n')

// Run-tier model ladders. Card complexity indexes into the active ladder;
// undefined = inherit the session model (the strongest available — e.g.
// Fable/Opus). Cards entering past Ready (cls null, never classified)
// always inherit the session model.
//   low    (run --low):  haiku / sonnet / opus
//   medium (default):    sonnet / opus / session
//   high   (run --high): opus / session / session
const LADDERS = {
  low: { low: 'haiku', medium: 'sonnet', high: 'opus' },
  medium: { low: 'sonnet', medium: 'opus', high: undefined },
  high: { low: 'opus', medium: undefined, high: undefined },
}
const ladder = LADDERS[input.tier || 'medium']
const tierFor = (cls) => (cls ? ladder[cls.complexity] : undefined)
// The classify router writes no code — haiku is fine except on --high runs.
const classifyModel = (input.tier || 'medium') === 'high' ? 'sonnet' : 'haiku'

const runLane = async (lane, card, model, history) => {
  const r = await agent(lanePrompt(lane, card), {
    label: `${lane}:#${card.number}`,
    phase: LANE[lane].phase,
    schema: STAGE_SCHEMA,
    ...(model ? { model } : {}),
  })
  const result = r || { status: 'failed', column: 'unknown', detail: `${lane} agent returned no result` }
  history.push({ lane, ...result })
  return result
}

const results = await pipeline(
  input.cards,
  // Stage 1: classify cards entering at Ready (router for model tiering)
  async (card) => {
    if (card.status !== 'Ready') return { card, cls: null }
    const cls = await agent(
      `Read GitHub issue #${card.number} ("${card.title}") — body and all comments — using gh issue view. ` +
      `Classify it: kind (feature|bug|docs|chore) and complexity (low|medium|high) judged by the scope of code change required.`,
      { label: `classify:#${card.number}`, phase: 'Classify', model: classifyModel, schema: CLASSIFY_SCHEMA }
    )
    return { card, cls }
  },
  // Stage 2: lane chain — entry point depends on the card's current column.
  // A non-'advanced' exit ends the chain; the next wave re-selects the card
  // from wherever it landed (the board is the loop state, not this script).
  async (prev, card) => {
    const history = []
    const model = tierFor(prev && prev.cls)
    let at = card.status

    if (at === 'Ready' && input.variant === 'full') {
      const b = await runLane('build', card, model, history)
      if (b.status !== 'advanced') return { number: card.number, history }
      at = 'QA'
    }
    // By design: qa-only boards have no Builder lane — Ready cards go
    // straight to the Tester (run.md "Lane mapping by variant").
    if (at === 'Ready' && input.variant === 'qa-only') at = 'QA'
    if (at === 'QA') {
      const q = await runLane('qa', card, model, history)
      if (q.status !== 'advanced') return { number: card.number, history }
      at = 'Review'
    }
    if (at === 'Review') {
      // Reviewer always on session model; serialized unless a human merges.
      const review = () => runLane('review', card, undefined, history)
      const reviewResult = input.humanApprovesMerge ? await review() : await withReviewLock(review)
      // The Review→human transition is gated on the exact-SHA handoff. A card
      // whose head moved, or whose SHA-bound check did not conclude success,
      // is NOT reported as merge-ready however clean the review read.
      const handoff = validateMergeHandoff(reviewResult)
      history[history.length - 1].mergeReady = handoff.mergeReady
      history[history.length - 1].handoffReason = handoff.reasonCode
      if (!handoff.mergeReady) {
        log(
          `#${card.number} stops in Review — not merge-ready (${handoff.reasonCode}); ` +
          `tested=${handoff.tested || 'none'} head=${handoff.current || 'unknown'} ` +
          `check=${QA_CHECK_CONTEXT}`
        )
      }
    }
    return { number: card.number, history }
  }
)

const summary = results.filter(Boolean).map((r) => {
  const last = r.history[r.history.length - 1] ||
    { lane: 'none', status: 'failed', column: 'unknown', detail: 'no lane ran' }
  return {
    number: r.number,
    finalStatus: last.status,
    lastLane: last.lane,
    column: last.column,
    detail: last.detail,
    mergeReady: last.mergeReady === true,
    handoffReason: last.handoffReason || null,
    prUrl: last.prUrl || null,
    lanesRun: r.history.map((h) => `${h.lane}:${h.status}`).join(' → ') || 'none',
  }
})
log(`wave complete (activation=${activationMode}): ${summary.length} cards — ` +
    summary.map((s) => `#${s.number}=${s.finalStatus}@${s.column}`).join(', '))
return { activationMode, cards: summary }
