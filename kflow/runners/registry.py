"""Dynamic discovery and registration of custom runner classes."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Dict, Iterable, Optional, Type

from .base import BaseRunner


class RunnerLoadError(RuntimeError):
    """Raised when a runner file cannot be imported or a class is missing."""


class RunnerRegistry:
    """Loads external ``.py`` files and registers their ``BaseRunner`` subclasses.

    Files are imported by absolute path with a unique module name, so two
    runner files may both define a class called ``Runner`` without clashing as
    modules - though registry *names* must still be unique across the project.
    """

    def __init__(self, console=None):
        self._classes: Dict[str, Type[BaseRunner]] = {}
        self._loaded_files: set = set()
        self.console = console

    # -- loading ----------------------------------------------------------

    def load_file(self, path) -> list:
        """Import ``path`` and register every ``BaseRunner`` subclass it defines.

        Returns the list of registry names discovered in this file.
        """
        path = Path(path).resolve()
        if path in self._loaded_files:
            return [n for n, c in self._classes.items()
                    if getattr(c, "__kflow_source__", None) == str(path)]
        if not path.exists():
            raise RunnerLoadError(f"runner file not found: {path}")

        module_name = f"kflow_runner_{abs(hash(str(path)))}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RunnerLoadError(f"cannot load runner file: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - surface any import-time error
            raise RunnerLoadError(f"error importing {path}: {exc}") from exc

        discovered = []
        for value in vars(module).values():
            if (isinstance(value, type)
                    and issubclass(value, BaseRunner)
                    and value is not BaseRunner):
                value.__kflow_source__ = str(path)  # type: ignore[attr-defined]
                self.register(value)
                discovered.append(value.registry_name())
        self._loaded_files.add(path)
        return discovered

    def load_files(self, paths: Iterable) -> None:
        for path in paths:
            self.load_file(path)

    # -- registration -----------------------------------------------------

    def register(self, cls: Type[BaseRunner]) -> None:
        name = cls.registry_name()
        existing = self._classes.get(name)
        if existing is not None and existing is not cls:
            raise RunnerLoadError(
                f"duplicate runner name {name!r}: defined in "
                f"{getattr(existing, '__kflow_source__', '?')} and "
                f"{getattr(cls, '__kflow_source__', '?')}"
            )
        self._classes[name] = cls

    # -- lookup -----------------------------------------------------------

    def get(self, name: str) -> Type[BaseRunner]:
        try:
            return self._classes[name]
        except KeyError:
            raise RunnerLoadError(
                f"runner {name!r} is not registered. Known runners: "
                f"{', '.join(self.names()) or '(none)'}"
            )

    def instantiate(self, name: str, config: Optional[dict] = None) -> BaseRunner:
        return self.get(name)(config or {})

    def names(self) -> list:
        return sorted(self._classes)

    def items(self):
        return sorted(self._classes.items())
