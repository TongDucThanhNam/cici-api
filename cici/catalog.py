"""Provider-aware access to model and generation-option configuration.

The root configuration keeps the original flat Cici registry for backward
compatibility and nests additional provider registries by provider name.  This
module is the single place that understands that shape.
"""
from __future__ import annotations

from typing import Any


class ConfigCatalog:
    """Read-only view over provider, model, and option registries."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def providers(self) -> list[str]:
        """Configured provider names, preserving configuration order."""

        return list(self.cfg.get("providers", {}).keys()) or ["cici"]

    def has_provider(self, provider: str) -> bool:
        return provider in self.providers()

    def section(self, section: str, provider: str) -> dict[str, Any]:
        """Return a provider's models/options section.

        Cici retains the legacy flat ``image``/``video`` shape.  Keys whose
        names are providers are removed from that view so older clients never
        mistake a nested provider registry for a generation kind.
        """

        data = self.cfg.get(section, {})
        provider_names = set(self.cfg.get("providers", {}) or {})
        if provider in data:
            return data[provider]
        return {key: value for key, value in data.items() if key not in provider_names}

    def resolve_model(self, kind: str, alias: str | None, provider: str) -> dict[str, Any]:
        """Resolve a model alias, using the configured default when omitted."""

        registry = self.section("models", provider).get(kind, {})
        resolved_alias = alias or registry.get("default")
        for option in registry.get("options", []):
            if option["alias"] == resolved_alias:
                return option
        valid = [option["alias"] for option in registry.get("options", [])]
        raise ValueError(
            f"Unknown model '{resolved_alias}' for kind '{kind}'. Valid: {valid}"
        )

    def resolve_option(
        self,
        kind: str,
        group: str,
        alias: str,
        provider: str,
    ) -> dict[str, Any]:
        """Resolve one ratio/style/duration option for a provider and kind."""

        options = self.section("options", provider).get(kind, {}).get(group, [])
        for option in options:
            if option["alias"] == alias:
                return option
        valid = [option["alias"] for option in options]
        raise ValueError(
            f"Unknown {group} '{alias}' for kind '{kind}'. Valid: {valid}"
        )

    def aliases(self, section: str, kind: str, group: str, provider: str) -> list[str]:
        """Return configured aliases for API validation and help output."""

        entries = self.section(section, provider).get(kind, {}).get(group, [])
        return [entry["alias"] for entry in entries]
