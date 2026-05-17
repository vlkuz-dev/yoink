from __future__ import annotations

import importlib


def test_package_imports() -> None:
    mod = importlib.import_module("yoink")
    assert hasattr(mod, "__version__")


def test_main_module_imports() -> None:
    mod = importlib.import_module("yoink.__main__")
    assert callable(getattr(mod, "main", None))


def test_provider_registry_autodiscovers_instagram() -> None:
    from yoink.core.registry import ProviderRegistry

    registry = ProviderRegistry.autodiscover()
    provider = registry.find("https://www.instagram.com/p/abc123/")
    assert provider is not None
    assert provider.name == "instagram"


def test_pipeline_module_imports() -> None:
    mod = importlib.import_module("yoink.core.pipeline")
    assert hasattr(mod, "Pipeline")
    assert hasattr(mod, "retry_async")


def test_bot_module_imports() -> None:
    mod = importlib.import_module("yoink.bot")
    assert callable(getattr(mod, "build_bot", None))
