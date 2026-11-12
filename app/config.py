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


# One shared copy that the rest of the code imports.
settings = Settings()  # type: ignore[call-arg]