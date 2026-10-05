# e2e fixture

A tiny shop on 127.0.0.1 used to prove the e2e wrapper end to end (https://github.com/Wladefant/super-board/issues/473).
It is not a product test suite.

To run it, use a directory that has the pinned packages installed (`pins.json`), then:

1. Copy `fixture/*` into it, `../e2e.config.template.ts` as `e2e.config.ts` and `../e2e.request-guard.template.ts` as `e2e.request-guard.ts`.
2. `SERVED_SHA=<40-hex sha> node serve.mjs`
3. Record once: `python e2e_run.py --dir <dir> --record --app-url http://127.0.0.1:4173`
4. Replay (the default): `python e2e_run.py --dir <dir> --app-url http://127.0.0.1:4173`
5. Receipt: `python e2e_receipt.py --report <dir>/.e2e/report.json --expected-sha <sha> --base-url http://127.0.0.1:4173`
