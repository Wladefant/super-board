// Tests must never read or write the operator's live per-bot send budget under ~/.veyyon/run.
// telegram-budget.test.ts builds its own SharedBudget in a temp directory.
process.env.VEYYON_TELEGRAM_BUDGET_DIR = "off";
