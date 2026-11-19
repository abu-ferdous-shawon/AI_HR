import os

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.core import secret_store as store
from app.core import sites
from app.core.secret_store import SecretsError


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HR_AI_MASTER_KEY", raising=False)
    monkeypatch.setenv("HR_AI_MASTER_KEY_FILE", str(tmp_path / "missing.key"))
    store.clear_key_cache()
    store.PLAINTEXT_SEEN.clear()
    yield
    store.clear_key_cache()
    store.PLAINTEXT_SEEN.clear()


def use_key(monkeypatch, *keys):
    monkeypatch.setenv("HR_AI_MASTER_KEY", ",".join(keys))
    store.clear_key_cache()


def new_key():
    return Fernet.generate_key().decode()


def test_a_secret_round_trips_and_the_stored_form_hides_it(monkeypatch):
    use_key(monkeypatch, new_key())
    token = store.encrypt("my-api-secret")
    assert token.startswith("enc:v1:") and "my-api-secret" not in token
    assert store.resolve_secret(token) == "my-api-secret"
    assert store.encrypt("my-api-secret") != token          # a fresh random IV each time


def test_plain_values_pass_through_and_are_noted(monkeypatch):
    assert store.resolve_secret("plain-value", "FRAPPE_API_KEY") == "plain-value"
    assert store.resolve_secret("", "GEMINI_API_KEY") == ""
    assert store.PLAINTEXT_SEEN == {"FRAPPE_API_KEY"}        # empty is not a secret


def test_an_encrypted_value_without_a_key_explains_what_to_do():
    with pytest.raises(SecretsError, match="genkey"):
        store.resolve_secret("enc:v1:abc")


def test_the_wrong_key_is_refused(monkeypatch):
    use_key(monkeypatch, new_key())
    token = store.encrypt("x")
    use_key(monkeypatch, new_key())
    with pytest.raises(SecretsError, match="master key is not the one"):
        store.decrypt(token)


def test_a_broken_master_key_is_refused(monkeypatch):
    use_key(monkeypatch, "not-a-real-key")
    with pytest.raises(SecretsError, match="not valid"):
        store.encrypt("x")


def test_keys_can_be_rotated(monkeypatch):
    old, new = new_key(), new_key()
    use_key(monkeypatch, old)
    old_token = store.encrypt("secret")
    use_key(monkeypatch, new, old)                           # new key first, old still accepted
    assert store.decrypt(old_token) == "secret"
    fresh = store.encrypt("secret")
    use_key(monkeypatch, new)                                # the old key is no longer needed
    assert store.decrypt(fresh) == "secret"
    with pytest.raises(SecretsError):
        store.decrypt(old_token)


def test_the_key_can_come_from_a_file(monkeypatch, tmp_path):
    path = tmp_path / "keys" / "master.key"
    monkeypatch.setenv("HR_AI_MASTER_KEY_FILE", str(path))
    assert store.main(["genkey", "--write", str(path)]) == 0
    store.clear_key_cache()
    assert store.decrypt(store.encrypt("hello")) == "hello"
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.main(["genkey", "--write", str(path)]) == 2     # never overwritten


def test_settings_decrypt_secrets_and_still_hide_them(monkeypatch):
    use_key(monkeypatch, new_key())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)   # ignore whatever this machine has set
    cfg = Settings(frappe_url="http://x", frappe_api_key=store.encrypt("the-key"),
                   frappe_api_secret="plain-secret", _env_file=None)
    assert cfg.frappe_api_key.get_secret_value() == "the-key"
    assert cfg.frappe_api_secret.get_secret_value() == "plain-secret"
    assert "the-key" not in repr(cfg) and "plain-secret" not in repr(cfg)
    assert store.PLAINTEXT_SEEN == {"FRAPPE_API_SECRET"}


def test_a_site_secret_variable_may_hold_an_encrypted_value(monkeypatch):
    use_key(monkeypatch, new_key())
    monkeypatch.setenv("T_ENC", store.encrypt("site-key"))
    assert sites._secret("a", {"api_key_env": "T_ENC"}, "api_key").get_secret_value() == "site-key"
    monkeypatch.setenv("T_ENC", "enc:v1:garbage")
    with pytest.raises(sites.SiteConfigError, match="T_ENC"):
        sites._secret("a", {"api_key_env": "T_ENC"}, "api_key")