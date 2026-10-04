import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import komo_login as kl  # noqa: E402

FAKE_PW = "not-a-real-secret"


def _runner(by_vault):
    def run(argv, **_):
        vault = argv[2].split()[0].removeprefix("bw-")
        out = by_vault.get(vault)
        if out is None:
            return SimpleNamespace(returncode=1, stdout="", stderr="locked")
        return SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
    return run


def _item(name=kl.ITEM_NAME, user=kl.DEFAULT_ACCOUNT, pw=FAKE_PW):
    return {"name": name, "login": {"username": user, "password": pw}}


def test_finds_matching_item_in_us():
    run = _runner({"us": [_item(user="other@x.com", pw="x"), _item()]})
    assert kl.fetch_google_credentials(run=run) == (kl.DEFAULT_ACCOUNT, FAKE_PW)


def test_falls_through_to_eu_when_us_locked():
    run = _runner({"us": None, "eu": [_item()]})
    assert kl.fetch_google_credentials(run=run)[1] == FAKE_PW


def test_account_match_is_case_insensitive_and_env_overridable(monkeypatch):
    monkeypatch.setenv("KOMO_GOOGLE_ACCOUNT", "Alt@Example.com")
    run = _runner({"us": [_item(user="alt@example.com")]})
    assert kl.fetch_google_credentials(run=run)[0] == "alt@example.com"


@pytest.mark.parametrize("vaults", [
    {"us": [], "eu": []},
    {"us": None, "eu": None},
    {"us": [_item(name="mail.google.com")], "eu": [_item(user="someone@else.com")]},
    {"us": [_item(pw="")], "eu": []},
])
def test_fail_closed_names_secret_without_leaking(vaults, monkeypatch):
    monkeypatch.delenv("KOMO_GOOGLE_ACCOUNT", raising=False)
    monkeypatch.setenv("KOMO_PASSWORD", "plaintext-env-must-be-ignored")
    with pytest.raises(kl.VaultError) as err:
        kl.fetch_google_credentials(run=_runner(vaults))
    msg = str(err.value)
    assert kl.ITEM_NAME in msg and kl.DEFAULT_ACCOUNT in msg
    assert FAKE_PW not in msg and "plaintext-env" not in msg


def test_unreadable_output_and_timeout_fail_closed():
    def bad_json(argv, **_):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    def hangs(argv, **_):
        raise subprocess.TimeoutExpired(argv, 60)

    for run in (bad_json, hangs):
        with pytest.raises(kl.VaultError):
            kl.fetch_google_credentials(run=run)


def test_main_returns_2_and_prints_only_secret_name(capsys, monkeypatch):
    monkeypatch.setattr(kl, "fetch_google_credentials", lambda *a, **k: (_ for _ in ()).throw(kl.VaultError("Missing secret: X")))
    monkeypatch.setattr(sys, "argv", ["komo_login.py"])
    assert kl.main() == 2
    assert "Missing secret: X" in capsys.readouterr().err


def test_extract_loopback_url():
    text = "Complete Google sign-in in your browser:\nhttp://127.0.0.1:53211/start?n=abc\n"
    assert kl.extract_loopback_url(text) == "http://127.0.0.1:53211/start?n=abc"
    assert kl.extract_loopback_url("nothing here") is None
