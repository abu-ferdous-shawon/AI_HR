import pytest

from app.core import roles, sites
from app.core.frappe_client import FrappeClient, get_service_client
from app.core.roles import RoleResult
from app.core.sites import SiteConfigError

YAML = """
default: alpha
sites:
  alpha:
    hosts: [alpha.example.com, localhost]
    frappe_url: https://alpha.example.com/
    api_key_env: T_ALPHA_KEY
    api_secret_env: T_ALPHA_SECRET
  beta:
    hosts: [beta.example.com]
    frappe_url: http://10.0.0.5:8001
    api_key_env: T_BETA_KEY
    api_secret_env: T_BETA_SECRET
    llm_model: another-model
    knowledge_packs: [erpnext_core]
    indexing_schedule: every:12
"""


@pytest.fixture
def two_sites(tmp_path, monkeypatch):
    for var in ("ALPHA_KEY", "ALPHA_SECRET", "BETA_KEY", "BETA_SECRET"):
        monkeypatch.setenv(f"T_{var}", f"value-{var}")
    path = tmp_path / "sites.yaml"
    path.write_text(YAML, encoding="utf-8")
    monkeypatch.setattr(sites.settings, "sites_file", str(path))
    sites.clear_site_cache()
    yield path
    sites.clear_site_cache()


def write(path, text, monkeypatch):
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sites.settings, "sites_file", str(path))
    sites.clear_site_cache()


def test_without_a_sites_file_there_is_one_default_site(monkeypatch):
    monkeypatch.setattr(sites.settings, "sites_file", "")
    sites.clear_site_cache()
    try:
        only = sites.get_default_site()
        assert list(sites.get_sites()) == ["default"]
        assert only.frappe_url == sites.settings.frappe_url.strip().rstrip("/")
    finally:
        sites.clear_site_cache()


def test_two_sites_load_with_per_site_overrides(two_sites):
    alpha, beta = sites.get_sites()["alpha"], sites.get_sites()["beta"]
    assert alpha.frappe_url == "https://alpha.example.com"            # trailing slash removed
    assert beta.llm_model == "another-model" and beta.knowledge_packs == ["erpnext_core"]
    assert beta.indexing_schedule == "every:12"
    assert alpha.llm_model == sites.settings.gemini_model               # inherits the global value
    assert sites.get_default_site().name == "alpha"


def test_the_host_header_picks_the_site(two_sites):
    assert sites.site_for_host("beta.example.com").name == "beta"
    assert sites.site_for_host("BETA.example.com:443").name == "beta"   # port and case ignored
    assert sites.site_for_host("unknown.example.com").name == "alpha"   # default
    assert sites.site_for_host(None).name == "alpha"
    assert sites.site_for_host("[::1]:8100").name == "alpha"


def test_secrets_never_appear_in_text(two_sites):
    beta = sites.get_sites()["beta"]
    assert "value-BETA" not in repr(beta) and "value-BETA" not in str(beta)
    assert beta.api_key.get_secret_value() == "value-BETA_KEY"


def test_a_missing_secret_names_the_variable(two_sites, monkeypatch):
    monkeypatch.delenv("T_BETA_SECRET")
    sites.clear_site_cache()
    with pytest.raises(SiteConfigError, match="T_BETA_SECRET"):
        sites.get_sites()


def test_a_key_written_in_the_file_instead_of_a_variable_name_is_refused(tmp_path, monkeypatch):
    write(tmp_path / "s.yaml", "sites:\n  a:\n    frappe_url: http://x\n    api_key: abc\n"
          "    api_secret_env: X\n", monkeypatch)
    try:
        with pytest.raises(SiteConfigError, match="api_key_env"):
            sites.get_sites()
    finally:
        sites.clear_site_cache()


@pytest.mark.parametrize("change, expected", [
    ("frappe_url: ftp://x", "http://"),
    ("indexing_schedule: daily", "every:<hours>"),
    ("hosts: [https://a.com/x]", "bare host names"),
    ("knowledge_packs: [Bad Pack]", "lower_snake_case"),
    ("llm_modl: typo", "Extra inputs"),
])
def test_invalid_settings_are_rejected(tmp_path, monkeypatch, change, expected):
    monkeypatch.setenv("T_K", "k")
    monkeypatch.setenv("T_S", "s")
    text = ("sites:\n  a:\n    frappe_url: http://x\n    api_key_env: T_K\n"
            f"    api_secret_env: T_S\n    {change}\n")
    write(tmp_path / "s.yaml", text, monkeypatch)
    try:
        with pytest.raises(SiteConfigError, match=expected):
            sites.get_sites()
    finally:
        sites.clear_site_cache()


def test_a_host_cannot_belong_to_two_sites(tmp_path, monkeypatch):
    monkeypatch.setenv("T_K", "k")
    monkeypatch.setenv("T_S", "s")
    one = "    frappe_url: http://x\n    api_key_env: T_K\n    api_secret_env: T_S\n    hosts: [same.com]\n"
    write(tmp_path / "s.yaml", f"sites:\n  a:\n{one}  b:\n{one}", monkeypatch)
    try:
        with pytest.raises(SiteConfigError, match="belongs to both"):
            sites.get_sites()
    finally:
        sites.clear_site_cache()


def test_clients_follow_the_current_site(two_sites):
    with sites.use_site("beta"):
        assert FrappeClient().base_url == "http://10.0.0.5:8001"
        assert FrappeClient().session.headers["Authorization"] == "token value-BETA_KEY:value-BETA_SECRET"
    assert FrappeClient().base_url == "https://alpha.example.com"
    assert sites.current_site().name == "alpha"


def test_each_site_has_its_own_service_client(two_sites):
    with sites.use_site("alpha"):
        a = get_service_client()
        assert get_service_client() is a                                 # built once
    with sites.use_site("beta"):
        assert get_service_client() is not a


def test_unknown_site_names_are_refused(two_sites):
    with pytest.raises(KeyError):
        with sites.use_site("nope"):
            pass


def test_the_role_cache_is_kept_apart_per_site(two_sites, monkeypatch):
    calls = []
    monkeypatch.setattr(roles.settings, "role_cache_seconds", 60)
    monkeypatch.setattr(roles, "_resolve",
                        lambda emp, creds, prof: calls.append(emp) or RoleResult(("X",), "key"))
    roles.clear_role_cache()
    try:
        with sites.use_site("alpha"):
            roles.resolve_roles("E1", profile=object())
            roles.resolve_roles("E1", profile=object())         # cached
        with sites.use_site("beta"):
            roles.resolve_roles("E1", profile=object())         # same ID, other site: not cached
    finally:
        roles.clear_role_cache()
    assert calls == ["E1", "E1"]

def test_chat_provider_uses_site_model_and_encrypted_dotenv_key(two_sites, monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    from app.core import secret_store
    from app.core.llm import get_llm
    from app.core.llm import gemini

    monkeypatch.setenv("HR_AI_MASTER_KEY", Fernet.generate_key().decode())
    secret_store.clear_key_cache()
    try:
        config = tmp_path / ".env"
        config.write_text("T_BETA_GEMINI=" + secret_store.encrypt("beta-key") + "\n")
        monkeypatch.setitem(sites.settings.model_config, "env_file", str(config))
        two_sites.write_text(YAML.replace("llm_model: another-model",
                                         "llm_model: another-model\n    gemini_api_key_env: T_BETA_GEMINI"))
        monkeypatch.setattr(gemini, "GeminiProvider", lambda **kwargs: kwargs)
        with sites.use_site("beta"):
            provider = get_llm()
            assert provider == {"model": "another-model", "api_key": "beta-key"}
            assert get_llm() is provider
        with sites.use_site("alpha"):
            assert get_llm()["model"] != provider["model"]
    finally:
        secret_store.clear_key_cache()
