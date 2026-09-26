## Linked Issue
<!-- Mandatory issue reference. For leaf/sub-issues, use a closing keyword, e.g. Fixes #123 or Closes Wladefant/super-board#123.
     IMPORTANT: If linking a parent issue/epic that has open sub-issues, DO NOT use closing keywords (Fixes/Closes/Resolves).
     Use non-closing references instead (Refs #123 or Part of #123) to prevent premature parent closure. -->
Fixes #
## Single-Deliverable Summary
<!-- Exactly one sentence describing the single deliverable. PRs must be small and atomic. -->


## Content & Exact Head
- Target Branch: `main`
- Authoritative Head SHA: 
- Diff Scope: 

## Verification Evidence
<!-- Concrete, verifiable proof: command executed, test output, browser verification, or CI run URL -->
- Verification Command: 
- Evidence / Run URL: 

## Convention & Baseline Checklist
- [ ] Single deliverable: Confined strictly to the linked issue scope (no bundled refactors)
- [ ] Merge commits only: Synced forward with `git merge origin/main --no-ff`; no rebase, no squash
- [ ] Git identity verified: Author and committer are `Wladimir Kirjanovs <wladefant@gmail.com>`
- [ ] Canonical labels applied (`kind:*` and `area:*`)
- [ ] Milestone assigned
- [ ] Scope & Size Guard respected (diff <= 500 lines or exemption documented)
- [ ] Linked issue format: Closing keyword (Fixes #N) for leaf issues; non-closing reference (Refs #N / Part of #N) if parent issue has open sub-issues
