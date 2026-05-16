from __future__ import annotations

import importlib
import pkgutil
from urllib.parse import urlsplit

from yoink.providers.base import Provider


def _normalize_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


class ProviderRegistry:
    def __init__(self) -> None:
        self._by_domain: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        if not isinstance(provider, Provider):
            raise TypeError(f"object {provider!r} does not satisfy Provider protocol")
        for domain in provider.domains:
            self._by_domain[_normalize_host(domain)] = provider

    def find(self, url: str) -> Provider | None:
        host = urlsplit(url).hostname
        if not host:
            return None
        normalized = _normalize_host(host)
        provider = self._by_domain.get(normalized)
        if provider is None:
            return None
        if not provider.can_handle(url):
            return None
        return provider

    @classmethod
    def autodiscover(cls, package: str = "yoink.providers") -> ProviderRegistry:
        registry = cls()
        pkg = importlib.import_module(package)
        for module_info in pkgutil.iter_modules(pkg.__path__):
            if module_info.name == "base":
                continue
            module = importlib.import_module(f"{package}.{module_info.name}")
            candidate = getattr(module, "provider", None)
            if candidate is None:
                continue
            if isinstance(candidate, Provider):
                registry.register(candidate)
        return registry
