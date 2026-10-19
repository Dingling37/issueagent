"""Non-secret preferences and OS credential storage, separate from Agent state."""
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from .security import protect_secret


# Adapter extension point: only protocols exercised by this project are exposed.
PROVIDERS = {
    "anthropic": "https://api.anthropic.com",
    "deepseek": "https://api.deepseek.com/anthropic",
}


@dataclass(frozen=True)
class ModelConfig:
    provider: str
    model: str
    api_key: str = field(default="", repr=False, compare=False)

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError("Unsupported provider; choose anthropic or deepseek")
        if not self.model.strip() or len(self.model) > 200:
            raise ValueError("A nonempty model ID (up to 200 characters) is required")


class Credentials:
    """Explicit OS backends: never accept keyrings.alt plaintext/file fallbacks."""
    def __init__(self):
        try:
            if sys.platform == "win32":
                from keyring.backends.Windows import WinVaultKeyring
                self.backend = WinVaultKeyring()
            elif sys.platform == "darwin":
                from keyring.backends.macOS import Keyring
                self.backend = Keyring()
            else:
                from keyring.backends.SecretService import Keyring
                self.backend = Keyring()
            if self.backend.priority <= 0:
                raise RuntimeError()
        except Exception:
            raise RuntimeError("System credential store unavailable; unlock or configure it before continuing") from None

    def get(self, provider):
        try:
            key = self.backend.get_password("issue-agent", provider)
            protect_secret(key)
            return key
        except Exception:
            raise RuntimeError("Unable to read system credentials") from None

    def set(self, provider, key):
        protect_secret(key)
        try:
            self.backend.set_password("issue-agent", provider, key)
        except Exception:
            raise RuntimeError("Unable to save system credentials; no plaintext fallback is allowed") from None


class ConfigStore:
    def __init__(self, path=None, credentials=None):
        base = Path(os.environ.get("APPDATA") or Path.home() / ".config")
        self.path = Path(path) if path else base / "issue-agent" / "config.json"
        self._credentials = credentials

    @property
    def credentials(self):
        if self._credentials is None:
            self._credentials = Credentials()
        return self._credentials

    def load(self):
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if set(data) != {"provider", "model"}:
                raise ValueError()
            return ModelConfig(data["provider"], data["model"])
        except Exception:
            raise ValueError("Invalid preferences file; run issue-agent config --edit to replace it") from None

    def resolve(self, config):
        key = self.credentials.get(config.provider)
        if not key:
            raise ValueError("API Key missing from system credentials; run issue-agent config --edit")
        protect_secret(key)
        return ModelConfig(config.provider, config.model, key)

    def save(self, config):
        # Write credentials first; a failed vault write never publishes preferences.
        key = config.api_key or self.credentials.get(config.provider)
        protect_secret(key)
        if key and (key in config.model or key in config.provider):
            raise ValueError("Provider/model must not contain the API Key")
        if config.api_key:
            self.credentials.set(config.provider, config.api_key)
        elif not self.credentials.get(config.provider):
            raise ValueError("An API Key is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"provider": config.provider, "model": config.model},
                                        indent=2), encoding="utf-8")
        temporary.replace(self.path)
