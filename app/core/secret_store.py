"""Secrets that are encrypted at rest.

A secret is stored as 'enc:v1:<token>' and decrypted here, inside the server.
A value without that prefix is used as it is, so migration can be gradual.

The master key comes from the HR_AI_MASTER_KEY variable or from a key file
(HR_AI_MASTER_KEY_FILE, default: secrets/master.key). Several keys may be listed
(one per line, or comma separated): the first encrypts, all of them decrypt.

    python -m app.core.secret_store genkey --write secrets/master.key
    python -m app.core.secret_store encrypt      # asks for the secret, prints enc:v1:...
    python -m app.core.secret_store check        # shows which secrets are still plain text
"""
import argparse
import getpass
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

log = logging.getLogger("hr_ai.secrets")

PREFIX = "enc:v1:"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_FILE = PROJECT_ROOT / "secrets" / "master.key"
GENKEY_HELP = "python -m app.core.secret_store genkey --write secrets/master.key"

# Names of secrets that were read as plain text. The server reports these at startup.
PLAINTEXT_SEEN: set[str] = set()


class SecretsError(Exception):
    """A secret cannot be encrypted or decrypted. The message says what to fix."""


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.strip().startswith(PREFIX)


def _key_file() -> Path:
    raw = os.environ.get("HR_AI_MASTER_KEY_FILE", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_KEY_FILE


def _warn_if_open(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o007:
        log.warning("The master key file %s can be read by every user on this machine. "
                    "Run: chmod 600 %s (or 640 with the service's group)", path, path)


def _key_lines() -> list[str]:
    from_env = os.environ.get("HR_AI_MASTER_KEY", "").strip()
    if from_env:
        return [k.strip() for k in from_env.split(",") if k.strip()]
    path = _key_file()
    if not path.is_file():
        return []
    _warn_if_open(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


@lru_cache
def _fernet() -> MultiFernet | None:
    keys = _key_lines()
    if not keys:
        return None
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError):
        raise SecretsError("The master key is not valid. Create a new one with: "
                           + GENKEY_HELP) from None


def clear_key_cache() -> None:
    _fernet.cache_clear()


def _need_key() -> MultiFernet:
    fernet = _fernet()
    if fernet is None:
        raise SecretsError("No master key was found. Create one with: " + GENKEY_HELP
                           + "  (or set HR_AI_MASTER_KEY / HR_AI_MASTER_KEY_FILE)")
    return fernet


def encrypt(plain: str) -> str:
    return PREFIX + _need_key().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    token = value.strip()[len(PREFIX):]
    try:
        return _need_key().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise SecretsError("A secret could not be decrypted: the master key is not the one "
                           "that encrypted it, or the value was damaged.") from None
    except UnicodeEncodeError:
        raise SecretsError("An encrypted value contains characters that are not allowed.") from None


def resolve_secret(value: str, label: str = "") -> str:
    """Decrypt an 'enc:v1:' value. Anything else is returned as it is (and noted as plain text)."""
    if is_encrypted(value):
        return decrypt(value)
    if label and str(value).strip():
        PLAINTEXT_SEEN.add(label)
    return value


# ---------- command line ----------

def _genkey(path_text: str) -> int:
    path = Path(path_text).expanduser()
    if path.exists():
        print(f"{path} already exists, so it was not touched. Delete it yourself only if you "
              "are sure: secrets encrypted with it cannot be recovered without it.")
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(Fernet.generate_key().decode("ascii") + "\n")
    print(f"Master key written to {path}\n"
          "Back it up somewhere safe and separate from .env. If it is lost, every encrypted "
          "secret must be created again.")
    return 0


def _encrypt_prompt() -> int:
    try:
        plain = getpass.getpass("Secret to encrypt (typing is hidden): ").strip()
        if not plain:
            print("Nothing was entered.")
            return 2
        token = encrypt(plain)
        assert decrypt(token) == plain
    except SecretsError as exc:
        print(exc)
        return 2
    print("\nPut this value in .env (or the environment) in place of the plain secret:\n")
    print(token)
    return 0


def _check() -> int:
    # When run with -m, settings and sites use the canonical module instance.
    from app.core.secret_store import PLAINTEXT_SEEN
    try:
        from app.core import sites
        loaded = sites.get_sites()
        fernet = _fernet()
    except Exception as exc:                 # SecretsError, SiteConfigError, ...
        print(f"Problem: {exc}")
        return 2
    source = ("HR_AI_MASTER_KEY" if os.environ.get("HR_AI_MASTER_KEY", "").strip()
              else str(_key_file()))
    print(f"Master key: {'found (' + source + ')' if fernet else 'not found'}")
    print(f"Sites loaded: {', '.join(sites.get_sites())}")
    if PLAINTEXT_SEEN:
        print("Still plain text: " + ", ".join(sorted(PLAINTEXT_SEEN)))
        print("Encrypt each with:  python -m app.core.secret_store encrypt")
        return 1
    print("All configured secrets are encrypted." if loaded else "No secrets found.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage encrypted secrets.")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("genkey", help="create a new master key file")
    gen.add_argument("--write", required=True, metavar="PATH")
    sub.add_parser("encrypt", help="encrypt one secret (asks for it, hidden)")
    sub.add_parser("check", help="show which secrets are still plain text")
    args = parser.parse_args(argv)
    if args.command == "genkey":
        return _genkey(args.write)
    if args.command == "encrypt":
        return _encrypt_prompt()
    return _check()


if __name__ == "__main__":
    sys.exit(main())
