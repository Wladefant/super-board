# Reference: Task Handle Recording and Continuation Lifecycle

Detailed command sequences for native Superboard task lifecycle operations.

## Continuation Driver Invocations

```bash
# Evaluate continuation state without mutating or sending Telegram
python ~/.veyyon/workflows/continuation_driver.py \
  --state-dir ~/.veyyon/superboard/state \
  --repo-root . \
  --summary

# Run with authorized Telegram notification
python ~/.veyyon/workflows/continuation_driver.py \
  --state-dir ~/.veyyon/superboard/state \
  --repo-root . \
  --notify-telegram \
  --telegram-send
```

## Native Work Recording Flow

```bash
# 1. Record dispatch of prepared ticket
python ~/.veyyon/workflows/worker_backend.py \
  --record-native \
  --run-id <run-id> \
  --task-handle "agent://<task-id>"

# 2. Complete ticket upon agent exit with authentic result
python ~/.veyyon/workflows/worker_backend.py \
  --complete-native \
  --run-id <run-id> \
  --task-handle "agent://<task-id>" \
  --result-file <path-to-result.json>
```
