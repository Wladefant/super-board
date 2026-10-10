---
name: stripe-accounts
description: "Manage one Stripe account per project (operator Google login), create restricted agent keys, store them in Bitwarden EU and shared-auth, pair and verify the Stripe CLI in test mode only."
---

> Source of truth: [`managed-skills/stripe-accounts/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/managed-skills/stripe-accounts/SKILL.md) in Wladefant/super-board. Edit it there and merge. Then `python scripts/install-managed-skills.py stripe-accounts` copies it to `~/.veyyon/profiles/default/agent/managed-skills/`. The installer backs up a local edit and then overwrites it.

# Stripe accounts

Use one Stripe account per project under the operator's Google login, wkirianov@gmail.com. Never share an account between projects. Shipnovo status: https://github.com/Wladefant/shipnovo/issues/824.

## Access route

1. Fast route (works today): Stripe CLI pairing. `stripe login --project-name <project> --non-interactive` prints a pairing URL and code. Send both to the operator via `telegram_message`: "Open the link, sign in with Google, pick account <project>, check code <X>, click Allow." Never block an eval cell on the completion step: poll `stripe config --list` with a short hidden subprocess and filter to profile names only.
2. Dashboard steps the CLI cannot do (new account, restricted keys): send the operator a short Telegram step list and have them paste each key into a file under `C:/Users/wkiri/.veyyon/shared-auth/` that you name. Never into chat.
3. Browser extension route (`browser` with `app: {"extension": true}`): not available in the installed Veyyon (https://github.com/Wladefant/veyyon/issues/518). Never fall back to CDP or remote debugging. Never type or store the Google password.

## Create a project account

1. Account menu (top left) > New account, named after the project. Shipnovo uses Germany.
2. Record account name and ID only.
3. Leave business verification/activation to the operator; list the fields Stripe asks for. Never invent business facts.

## Create keys

1. Select the project account, test mode. Developers > API keys > Create restricted key, name `agents-YYYY-MM-DD`.
2. Write: Customers, Products, Prices, Checkout Sessions, Subscriptions, Billing Portal, Webhook Endpoints. Read: Invoices, Balance (Balance read enables CLI verification). Everything else off.
3. Repeat in live mode as a restricted key. Do not activate the account to get keys; if Stripe blocks live keys, record the operator step.

## Storage

Read skill `bitwarden-cli-two-vaults` first; use `bw-us` (the operator's main vault), never `bw-eu` for new items and never plain `bw`.

- One Bitwarden US item per project and mode: `Stripe test - <Project>`, `Stripe live - <Project>` (the names used on https://github.com/Wladefant/shipnovo/issues/824), in collection `<Project>` if it exists. Secret/restricted key, publishable key and webhook signing secret go in that item.
- Files (lowercase slug, user-only access): `C:/Users/wkiri/.veyyon/shared-auth/stripe_<project>_<test|live>_<restricted|publishable>.txt`.
- Shipnovo test files: `stripe_shipnovo_test_secret_key.txt`, `stripe_shipnovo_test_publishable_key.txt`, and `stripe_shipnovo_test_webhook.txt` in that shared-auth directory.
- Never overwrite an existing key without checking its purpose. Verify storage without printing values. Report only item names and paths.

## Verify the CLI

`stripe balance retrieve --project-name <project>` in test mode, never `--live`. Proof is `livemode: false` in the returned object, not the exit code. Never print `stripe config --list` or credentials.

### Match the account before provisioning

CLI pairing can select a different sandbox from the operator's app keys. The app key's account ID is the source of truth.

1. Load the stored test secret key into `STRIPE_API_KEY` inside the subprocess environment. Never print it or pass its value in tool arguments.
2. Run `stripe get /v1/account --project-name <project>` and `stripe balance retrieve --project-name <project>` with that environment.
3. Confirm the expected account ID and `livemode: false` before creating products, prices, or webhooks.
4. Verify the publishable key belongs to the same sandbox. Create an unattached test payment method with `tok_visa`, then retrieve it with the secret key. Create no charge.
5. If paired CLI credentials point elsewhere, stop using them. Provision only in the app key's account.
6. Remove the wrong profile with `stripe logout --project-name <project>` after any authorized endpoint cleanup. Use `STRIPE_API_KEY` for later CLI calls.

Shipnovo's verified test account is `acct_1UObjMARC8ETrcbd`, named `Shipnovo sandbox`. Initial pairing selected `acct_1UO2yTANB1Kr0Rgu`, named `New business`. Do not provision Shipnovo there.

## Safety

- No live charges, no redeeming credits or resets, no bank or payout changes.
- Never put Shipnovo live keys into Dokploy or enable live billing without the operator's word.
- Test keys go into an app only if it has a verified test/staging path; otherwise store only.
- A project key works only inside its account; it cannot create other accounts.
- Every subprocess has a timeout and a hidden window. Never log or post secrets.

## Completion record

Report account names, IDs, test/live availability, key types, modes, permissions, vault item names, file paths, CLI verification, and the exact operator steps still open. Never claim a pending step succeeded.
