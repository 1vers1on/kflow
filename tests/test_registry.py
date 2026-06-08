"""Tests for dynamic runner discovery (RunnerRegistry)."""

from __future__ import annotations

import pytest

from kflow.runners.base import BaseRunner
from kflow.runners.registry import RunnerLoadError, RunnerRegistry


def _write_runner(tmp_path, name, body="        pass", cls="MyRunner"):
    p = tmp_path / name
    p.write_text(
        "from kflow.runners import BaseRunner\n\n\n"
        f"class {cls}(BaseRunner):\n"
        f"    description = 'd'\n"
        f"    def apply(self, ctx):\n{body}\n"
    )
    return p


def test_load_file_discovers_subclass(tmp_path):
    reg = RunnerRegistry()
    discovered = reg.load_file(_write_runner(tmp_path, "r.py"))
    assert discovered == ["MyRunner"]
    assert "MyRunner" in reg.names()


def test_load_file_not_found(tmp_path):
    with pytest.raises(RunnerLoadError):
        RunnerRegistry().load_file(tmp_path / "nope.py")


def test_load_file_import_error(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("import does_not_exist_module_xyz\n")
    with pytest.raises(RunnerLoadError):
        RunnerRegistry().load_file(p)


def test_load_file_idempotent(tmp_path):
    reg = RunnerRegistry()
    p = _write_runner(tmp_path, "r.py")
    first = reg.load_file(p)
    second = reg.load_file(p)  # already loaded -> returns same names, no re-import
    assert first == second == ["MyRunner"]


def test_duplicate_runner_name_raises(tmp_path):
    reg = RunnerRegistry()
    reg.load_file(_write_runner(tmp_path, "a.py", cls="Dup"))
    with pytest.raises(RunnerLoadError):
        reg.load_file(_write_runner(tmp_path, "b.py", cls="Dup"))


def test_instantiate_passes_config(tmp_path):
    reg = RunnerRegistry()
    reg.load_file(_write_runner(tmp_path, "r.py"))
    inst = reg.instantiate("MyRunner", {"k": "v"})
    assert isinstance(inst, BaseRunner)
    assert inst.config == {"k": "v"}


def test_get_unknown_runner_raises(tmp_path):
    with pytest.raises(RunnerLoadError):
        RunnerRegistry().get("Ghost")


def test_names_and_items_sorted(tmp_path):
    reg = RunnerRegistry()
    reg.load_file(_write_runner(tmp_path, "a.py", cls="Bravo"))
    reg.load_file(_write_runner(tmp_path, "b.py", cls="Alpha"))
    assert reg.names() == ["Alpha", "Bravo"]
    assert [n for n, _ in reg.items()] == ["Alpha", "Bravo"]


def test_registry_name_prefers_custom_name():
    class Custom(BaseRunner):
        name = "the-name"

    assert Custom.registry_name() == "the-name"


def test_registry_name_defaults_to_class_name():
    class Plain(BaseRunner):
        pass

    assert Plain.registry_name() == "Plain"


def test_base_runner_not_registered(tmp_path):
    """A file importing BaseRunner without subclassing registers nothing."""
    p = tmp_path / "empty.py"
    p.write_text("from kflow.runners import BaseRunner\nX = 1\n")
    reg = RunnerRegistry()
    assert reg.load_file(p) == []


def test_reload_hook_defaults_to_apply():
    calls = []

    class R(BaseRunner):
        def apply(self, ctx):
            calls.append("apply")

    R().reload(ctx=None)
    assert calls == ["apply"]
