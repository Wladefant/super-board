#!/usr/bin/env python3
"""Sign the Komo CLI in for the self-hosted Komo (https://komo.wladefant.de).

The Google account credentials come from the operator's Bitwarden vaults
(bw-us, then bw-eu), never from a plaintext file or an env var. If the lookup
fails the script stops and names the missing secret. It has no plaintext
fallback. Output carries secret NAMES only, never values.

Usage: python infra/komo/komo_login.py [--project KEY] [--endpoint URL]
Env:   KOMO_GOOGLE_ACCOUNT  Google account name to look up (default below)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading

DEFAULT_ACCOUNT = "wkirianov@gmail.com"
ITEM_NAME = "accounts.google.com"
VAULTS = ("us", "eu")
KOMO_PACKAGE = "@tjcages/komo@0.5.0"
DEFAULT_ENDPOINT = "https://komo.wladefant.de"
DEFAULT_PROJECT = "polysimulator-design"


class VaultError(RuntimeError):
    """A vault lookup failed. The message names the secret, never its value."""


def secret_label(account: str) -> str:
    return f"Bitwarden login '{ITEM_NAME}' for {account}"


def _bw_list(vault: str, run) -> list[dict]:
    proc = run(
        ["cmd", "/c", f"bw-{vault} list items --search {ITEM_NAME}"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise VaultError(f"bw-{vault} failed (exit {proc.returncode}); is the vault unlocked?")
    try:
        items = json.loads(proc.stdout)
    except ValueError:
        raise VaultError(f"bw-{vault} returned unreadable output; is the vault unlocked?") from None
    return items if isinstance(items, list) else []


def fetch_google_credentials(account: str | None = None, run=subprocess.run) -> tuple[str, str]:
    """Return (username, password) of the Google login from the vault, or raise VaultError."""
    account = account or os.environ.get("KOMO_GOOGLE_ACCOUNT") or DEFAULT_ACCOUNT
    problems = []
    for vault in VAULTS:
        try:
            items = _bw_list(vault, run)
        except (VaultError, OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{vault}: {type(exc).__name__}")
            continue
        for item in items:
            login = item.get("login") or {}
            if (
                item.get("name") == ITEM_NAME
                and (login.get("username") or "").lower() == account.lower()
                and login.get("password")
            ):
                return login["username"], login["password"]
    detail = f" ({'; '.join(problems)})" if problems else ""
    raise VaultError(f"Missing secret: {secret_label(account)} not found in the vaults{detail}. Nothing was used as a fallback.")


_URL_RE = re.compile(r"https?://127\.0\.0\.1:\d+/\S*")


def extract_loopback_url(text: str) -> str | None:
    match = _URL_RE.search(text)
    return match.group(0) if match else None


def _google_sign_in(url: str, username: str, password: str, timeout_ms: int = 90000) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = None
        for channel in ("chrome", "msedge", None):
            try:
                browser = pw.chromium.launch(channel=channel, headless=True) if channel else pw.chromium.launch(headless=True)
                break
            except Exception:
                continue
        if browser is None:
            raise RuntimeError("No Chromium browser could be launched.")
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url)
        # The loopback page opens Google sign-in in a popup window.
        with context.expect_page() as popup_info:
            page.get_by_role("button", name="Continue with Google").click()
        popup = popup_info.value
        popup.set_default_timeout(timeout_ms)
        popup.wait_for_url(re.compile(r"accounts\.google\.com"))
        popup.fill("#identifierId", username)
        popup.keyboard.press("Enter")
        popup.fill('input[name="Passwd"]', password)
        popup.keyboard.press("Enter")
        # A consent screen or a challenge (passkey, phone, TOTP) may follow; the popup closes on success.
        for _ in range(30):
            page.wait_for_timeout(2000)
            if popup.is_closed():
                break
            if "challenge" in popup.url:
                raise RuntimeError(
                    "Google asked for a second step (" + popup.url.split("?")[0].rsplit("/", 1)[-1] + "). "
                    "This vault login has no TOTP secret, so it cannot be completed unattended."
                )
            for label in ("Continue", "Allow", "I understand"):
                btn = popup.get_by_role("button", name=label)
                if btn.count():
                    btn.first.click()
                    break
        page.wait_for_timeout(3000)
        browser.close()


def login(project: str, endpoint: str) -> int:
    username, password = fetch_google_credentials()
    print(f"vault: found {secret_label(username)} (value not shown)")
    cmd = ["npx", "-y", KOMO_PACKAGE, "login", "--no-open", "--endpoint", endpoint, "--project", project]
    proc = subprocess.Popen(
        ["cmd", "/c", " ".join(cmd)] if os.name == "nt" else cmd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    url_holder: list[str] = []
    stderr_lines: list[str] = []

    def pump() -> None:
        for line in proc.stderr:
            stderr_lines.append(line)
            found = extract_loopback_url(line)
            if found and not url_holder:
                url_holder.append(found)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    for _ in range(120):
        if url_holder or proc.poll() is not None:
            break
        thread.join(0.5)
    if not url_holder:
        proc.kill()
        print("komo login did not print a sign-in URL", file=sys.stderr)
        return 1
    try:
        _google_sign_in(url_holder[0], username, password)
    except Exception as exc:
        proc.kill()
        print(f"sign-in failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    proc.wait(timeout=60)
    print(proc.stdout.read().strip())
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    args = parser.parse_args()
    try:
        return login(args.project, args.endpoint)
    except VaultError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
