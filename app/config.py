from typing import Literal
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings for the backend, loaded from environment variables or .env."""

    # Read the .env file in the folder where you run the program.
    # extra="ignore" means unknown lines in .env do not cause errors.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Frappe connection (required: no default, so a missing value is an error)
    frappe_url: str
    frappe_api_key: SecretStr
    frappe_api_secret: SecretStr

    # LLM settings (used from Stage 2; defaults let Stage 1 run without them)
    llm_provider: str = "gemini"
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = "gemini-2.5-flash"
    # Testing shortcut: lets a browser on this machine chat as any employee.
    # Never turn this on for a server that other people can reach.
    dev_mode: bool = False
    policy_dir: str = "policies"
        # Language the policy documents are written in (the model searches in it)
    policy_language: str = "English"

    # Registry profile: a name in app/registry/profiles/ or a file path.
    registry_profile: str = "frappe_hrms_v16"

    # How long a person's resolved roles are remembered (0 = ask every time).
    role_cache_seconds: int = 60
    # Employee IDs (comma separated) who always count as HR, whatever their roles.
    # HR role names themselves are set in the registry profile (access.hr_roles).
    hr_admin_employees: str = ""
    # Speech (off by default; needs requirements-speech.txt)
    speech_enabled: bool = False
    speech_device: str = "auto"                 # auto, cpu, cuda:0
    stt_model: str = "openai/whisper-large-v3"
    tts_provider: str = "indic_parler"          # indic_parler or mms
    tts_model: str = "ai4bharat/indic-parler-tts"
    tts_model_bn: str = "Tanmoyshome/bn-parler-tts"     # Bangladeshi Bangla
    tts_model_en: str = "ai4bharat/indic-parler-tts"    # English
    tts_seed: int = 0  
    tts_voice_bn: str = ("Aditi speaks in a clear, moderate-paced voice with a very "
                         "clear recording and no background noise.")
    tts_voice_en: str = ("A female speaker delivers clear, moderate-paced English speech "
                         "with a very clear recording and no background noise.")
    tts_transliterate_banglish: bool = True
    tts_max_chars: int = 800
    speech_max_concurrent: int = 1
    speech_rate_per_minute: int = 20

    # How the ERP is called for a person's data:
    #   user_session    as the logged-in person, so the ERP enforces their own roles,
    #                   user permissions and field permissions (default)
    #   service_account with the integration API key; only this app's rules apply
    frappe_auth_mode: Literal["user_session", "service_account"] = "user_session"

    # Gateway limits (0 turns a limit off).
    rate_limit_chat_per_minute: int = 20
    rate_limit_auth_fail_per_minute: int = 10   # failed sign-ins per IP
    max_request_bytes: int = 20_000_000
    # True only behind your own web server on this machine, which must send X-Real-IP.
    trust_proxy: bool = False

    # Audit log (one JSON line per event) and application log. "-" = standard output.
    audit_log_file: str = "logs/audit.jsonl"
    audit_log_max_mb: int = 10
    audit_log_backups: int = 10
    log_json: bool = True
    log_file: str = ""
    log_max_mb: int = 10
    log_backups: int = 5

    # Deployment (Phase 6)
    environment: str = "development"   # "production" on the VM
    allowed_origins: str = ""          # extra exact browser origins, comma separated
    log_level: str = "INFO"


# One shared copy that the rest of the code imports.
settings = Settings()  # type: ignore[call-arg]