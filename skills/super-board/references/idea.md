# super-board idea

Turn one idea sentence into judged issues. Do not file drafts directly.

1. Read the target project's scope, branch route, milestone and test surface.
2. Read the active host profile's `modelRoles`. Select allowed draft and judge models. Do not add a provider, buy credits, or use legacy grok routing.
3. Run `python scripts/super-board-idea.py "<sentence>" --context "<scope, branch, milestone, test surface>" --draft-model "<configured model>" --judge-model "<configured judge>" --output idea-result.json`.
4. Inspect the JSON drafts and rewrite history. The command defaults to a dry-run against `Wladefant/super-board`. Each fresh judge completion checks scope and measurable outcomes. The existing runtime `normalize_intake` is the only executable issue-quality authority. Failed candidates return to the draft model with the exact lint failures. After three unsuccessful rewrites, the command stops without filing.
5. File only with explicit target authorization. Add `--file --repo owner/repo --project "<project title>"` and any `--label "<existing label>"` values. The entire batch passes lint again before the first GitHub write. GitHub creates the passing issues on that Project. The output file retains successful issue URLs if a later write fails. Do not blindly repeat a partially filed run.

Both model processes run without tools, extensions, rules, skills or session persistence. They cannot file issues. Only the lint-gated filing phase can do that. Model calls and GitHub commands have bounded timeouts.
