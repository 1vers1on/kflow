"""Tests for the runner authoring helpers (kflow.runners.helpers)."""

from __future__ import annotations

import base64
import string

import yaml

from kflow.runners import helpers


def test_generate_secret_length_and_alphabet():
    s = helpers.generate_secret(40)
    assert len(s) == 40
    assert all(c in (string.ascii_letters + string.digits) for c in s)


def test_generate_secret_custom_alphabet():
    s = helpers.generate_secret(50, alphabet="ab")
    assert set(s) <= {"a", "b"}


def test_generate_secret_is_random():
    assert helpers.generate_secret(32) != helpers.generate_secret(32)


def test_b64_roundtrip():
    assert base64.b64decode(helpers.b64("hunter2")).decode() == "hunter2"


def test_configmap_manifest_shape():
    doc = yaml.safe_load(helpers.configmap_manifest("cm", "ns", {"a": 1},
                                                    labels={"x": "y"}))
    assert doc["kind"] == "ConfigMap"
    assert doc["metadata"]["name"] == "cm"
    assert doc["metadata"]["namespace"] == "ns"
    assert doc["metadata"]["labels"]["x"] == "y"
    assert doc["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "kflow"
    # values are stringified
    assert doc["data"]["a"] == "1"


def test_secret_manifest_string_data_default():
    doc = yaml.safe_load(helpers.secret_manifest("s", "ns", {"PASSWORD": "p"}))
    assert doc["kind"] == "Secret"
    assert doc["type"] == "Opaque"
    assert doc["stringData"]["PASSWORD"] == "p"
    assert "data" not in doc


def test_secret_manifest_data_base64():
    doc = yaml.safe_load(
        helpers.secret_manifest("s", "ns", {"PASSWORD": "p"}, string_data=False)
    )
    assert base64.b64decode(doc["data"]["PASSWORD"]).decode() == "p"
    assert "stringData" not in doc


def test_wait_for_returns_true_when_predicate_true():
    assert helpers.wait_for(lambda: True, timeout=1, interval=0.01) is True


def test_wait_for_times_out():
    assert helpers.wait_for(lambda: False, timeout=0.05, interval=0.01) is False


def test_wait_for_eventually_true():
    state = {"n": 0}

    def pred():
        state["n"] += 1
        return state["n"] >= 3

    assert helpers.wait_for(pred, timeout=1, interval=0.01) is True
    assert state["n"] >= 3
