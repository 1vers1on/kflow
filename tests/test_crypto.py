"""Tests for manifest encryption: the crypto primitives, the key ring, the
loader/engine integration, and the ``kflow crypto`` CLI."""

from __future__ import annotations

import base64

import pytest
from click.testing import CliRunner

from kflow import crypto
from kflow.cli import cli
from kflow.models import ConfigError


SECRET_YAML = (
    "apiVersion: v1\n"
    "kind: Secret\n"
    "metadata:\n  name: db-credentials\n"
    "stringData:\n  password: hunter2\n"
)


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #


def test_generate_key_is_valid_fernet():
    key = crypto.generate_key()
    raw = base64.urlsafe_b64decode(key)
    assert len(raw) == 32
    # round-trips through normalize unchanged
    assert crypto.normalize_key(key) == key


def test_generate_key_is_random():
    assert crypto.generate_key() != crypto.generate_key()


def test_derive_key_is_deterministic():
    a = crypto.derive_key("correct horse battery staple")
    b = crypto.derive_key("correct horse battery staple")
    assert a == b
    assert crypto.normalize_key(a) == a


def test_derive_key_salt_changes_output():
    a = crypto.derive_key("pw", salt=b"salt-a")
    b = crypto.derive_key("pw", salt=b"salt-b")
    assert a != b


def test_derive_key_rejects_empty_passphrase():
    with pytest.raises(crypto.EncryptionError):
        crypto.derive_key("")


def test_normalize_key_accepts_standard_base64():
    raw = b"0" * 32
    std = base64.b64encode(raw).decode()
    assert crypto.normalize_key(std) == base64.urlsafe_b64encode(raw).decode()


def test_normalize_key_rejects_wrong_length():
    with pytest.raises(crypto.EncryptionError):
        crypto.normalize_key(base64.urlsafe_b64encode(b"short").decode())


def test_normalize_key_rejects_garbage():
    with pytest.raises(crypto.EncryptionError):
        crypto.normalize_key("!!!! not base64 !!!!")


def test_key_fingerprint_stable_and_short():
    key = crypto.generate_key()
    fp = crypto.key_fingerprint(key)
    assert fp == crypto.key_fingerprint(key)
    assert len(fp) == 12
    assert fp != crypto.key_fingerprint(crypto.generate_key())


def test_env_var_for():
    assert crypto.env_var_for("default") == "KFLOW_KEY"
    assert crypto.env_var_for(None) == "KFLOW_KEY"
    assert crypto.env_var_for("prod") == "KFLOW_KEY_PROD"


# --------------------------------------------------------------------------- #
# Envelope round-trip
# --------------------------------------------------------------------------- #


def test_encrypt_decrypt_round_trip():
    key = crypto.generate_key()
    env = crypto.encrypt_bytes(SECRET_YAML.encode(), key, name="secret.yaml")
    assert crypto.is_encrypted(env)
    assert crypto.decrypt_bytes(env, key).decode() == SECRET_YAML


def test_envelope_records_metadata():
    key = crypto.generate_key()
    text = crypto.encrypt_bytes(b"data", key, kid="prod", name="x.yaml")
    env = crypto.Envelope.loads(text)
    assert env.kid == "prod"
    assert env.name == "x.yaml"
    assert env.alg == crypto.ALG_FERNET
    assert env.version == crypto.ENVELOPE_VERSION
    assert env.created  # timestamp present


def test_envelope_dumps_starts_with_magic():
    key = crypto.generate_key()
    text = crypto.encrypt_bytes(b"data", key)
    assert text.splitlines()[0] == crypto.MAGIC


def test_ciphertext_is_not_plaintext():
    key = crypto.generate_key()
    text = crypto.encrypt_bytes(SECRET_YAML.encode(), key)
    assert "hunter2" not in text
    assert "password" not in text


def test_decrypt_with_wrong_key_fails():
    text = crypto.encrypt_bytes(b"data", crypto.generate_key())
    with pytest.raises(crypto.EncryptionError):
        crypto.decrypt_bytes(text, crypto.generate_key())


def test_tampered_ciphertext_fails():
    key = crypto.generate_key()
    text = crypto.encrypt_bytes(b"important data", key)
    lines = text.splitlines()
    # Flip a character in the payload (last line).
    payload = list(lines[-1])
    payload[0] = "A" if payload[0] != "A" else "B"
    lines[-1] = "".join(payload)
    with pytest.raises(crypto.EncryptionError):
        crypto.decrypt_bytes("\n".join(lines), key)


def test_loads_rejects_non_envelope():
    with pytest.raises(crypto.EncryptionError):
        crypto.Envelope.loads("apiVersion: v1\nkind: Secret\n")


def test_loads_rejects_missing_separator():
    bad = f"{crypto.MAGIC}\nversion: 1\nkid: default\ndeadbeef\n"
    with pytest.raises(crypto.EncryptionError):
        crypto.Envelope.loads(bad)


def test_loads_rejects_unknown_version():
    key = crypto.generate_key()
    text = crypto.encrypt_bytes(b"x", key).replace("version: 1", "version: 99")
    with pytest.raises(crypto.EncryptionError):
        crypto.Envelope.loads(text)


def test_is_encrypted_handles_leading_blank_lines():
    key = crypto.generate_key()
    text = "\n\n" + crypto.encrypt_bytes(b"x", key)
    assert crypto.is_encrypted(text)
    assert crypto.decrypt_bytes(text, key) == b"x"


def test_is_encrypted_false_for_plain_yaml():
    assert not crypto.is_encrypted(SECRET_YAML)


def test_is_encrypted_file(tmp_path):
    key = crypto.generate_key()
    enc = tmp_path / "s.enc"
    enc.write_text(crypto.encrypt_bytes(b"x", key))
    plain = tmp_path / "s.yaml"
    plain.write_text(SECRET_YAML)
    assert crypto.is_encrypted_file(enc)
    assert not crypto.is_encrypted_file(plain)
    assert not crypto.is_encrypted_file(tmp_path / "missing")


# --------------------------------------------------------------------------- #
# .env parsing
# --------------------------------------------------------------------------- #


def test_parse_dotenv_variants():
    text = (
        "# a comment\n"
        "\n"
        "export KFLOW_KEY='abc'\n"
        'KFLOW_KEY_PROD="xyz"\n'
        "PLAIN=123\n"
        "noequalsign\n"
    )
    parsed = crypto.parse_dotenv(text)
    assert parsed == {"KFLOW_KEY": "abc", "KFLOW_KEY_PROD": "xyz", "PLAIN": "123"}


def test_load_dotenv_file_missing(tmp_path):
    assert crypto.load_dotenv_file(tmp_path / "nope.env") == {}


# --------------------------------------------------------------------------- #
# KeyRing
# --------------------------------------------------------------------------- #


def test_keyring_add_and_decrypt():
    key = crypto.generate_key()
    ring = crypto.KeyRing()
    ring.add("default", key)
    text = ring.encrypt(b"hi")
    assert ring.decrypt(text) == b"hi"


def test_keyring_primary_prefers_default():
    ring = crypto.KeyRing()
    ring.add("prod", crypto.generate_key())
    ring.add("default", crypto.generate_key())
    assert ring.primary_kid == "default"


def test_keyring_primary_falls_back_to_first():
    ring = crypto.KeyRing()
    ring.add("staging", crypto.generate_key())
    ring.add("prod", crypto.generate_key())
    assert ring.primary_kid == "staging"


def test_keyring_selects_key_by_kid():
    ring = crypto.KeyRing()
    k_def, k_prod = crypto.generate_key(), crypto.generate_key()
    ring.add("default", k_def)
    ring.add("prod", k_prod)
    text = crypto.encrypt_bytes(b"secret", k_prod, kid="prod")
    assert ring.decrypt(text) == b"secret"


def test_keyring_falls_back_to_trying_all_keys():
    # Envelope names a kid the ring doesn't have, but a key on the ring works.
    real = crypto.generate_key()
    ring = crypto.KeyRing()
    ring.add("default", real)
    text = crypto.encrypt_bytes(b"secret", real, kid="renamed-kid")
    assert ring.decrypt(text) == b"secret"


def test_keyring_decrypt_without_keys_errors():
    ring = crypto.KeyRing()
    text = crypto.encrypt_bytes(b"x", crypto.generate_key())
    with pytest.raises(crypto.EncryptionError):
        ring.decrypt(text)


def test_keyring_require_missing_kid():
    ring = crypto.KeyRing()
    with pytest.raises(crypto.EncryptionError):
        ring.require("ghost")


def test_keyring_from_mapping():
    k = crypto.generate_key()
    ring = crypto.KeyRing.from_mapping({"KFLOW_KEY": k, "KFLOW_KEY_PROD": crypto.generate_key()})
    assert ring.kids == ["default", "prod"]
    assert ring.get("default") == k


def test_keyring_from_mapping_ignores_invalid():
    ring = crypto.KeyRing.from_mapping({"KFLOW_KEY": "not-a-key"})
    assert not ring


def test_keyring_from_environment_reads_dotenv(tmp_path, monkeypatch):
    key = crypto.generate_key()
    (tmp_path / ".env").write_text(f"KFLOW_KEY={key}\n")
    monkeypatch.delenv("KFLOW_KEY", raising=False)
    ring = crypto.KeyRing.from_environment([tmp_path])
    assert ring.get("default") == key


def test_keyring_env_overrides_dotenv(tmp_path, monkeypatch):
    file_key = crypto.generate_key()
    env_key = crypto.generate_key()
    (tmp_path / ".env").write_text(f"KFLOW_KEY={file_key}\n")
    monkeypatch.setenv("KFLOW_KEY", env_key)
    ring = crypto.KeyRing.from_environment([tmp_path])
    assert ring.get("default") == env_key


# --------------------------------------------------------------------------- #
# Loader integration
# --------------------------------------------------------------------------- #


def _write_project(tmp_path, *, encrypted=True, key=None):
    """Create a minimal project whose web resource applies an encrypted secret."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "state").mkdir()
    key = key or crypto.generate_key()
    (proj / ".env").write_text(f"KFLOW_KEY={key}\n")
    enc = crypto.encrypt_bytes(SECRET_YAML.encode(), key, name="secret.yaml")
    (proj / "secret.yaml.enc").write_text(enc)
    enc_line = "    encrypted: true\n" if encrypted else ""
    (proj / "web.yaml").write_text(
        "kflow:\n  version: v1\n  kind: ResourceDefinition\n"
        "name: web\nnamespace: apps\nphase: app\n"
        "steps:\n"
        "  - name: secret\n"
        f"{enc_line}"
        "    manifests:\n      - secret.yaml.enc\n"
    )
    config = proj / "kflow.yaml"
    config.write_text(
        "kflow:\n  version: v1\n  kind: Config\n"
        f"state:\n  dir: {proj / 'state'}\n"
        "phases:\n  - name: app\n"
        "resources:\n  - web.yaml\n"
    )
    return config, key


def test_loader_parses_encrypted_flag(tmp_path):
    from kflow.loader import load_root_config
    config, _ = _write_project(tmp_path)
    cfg = load_root_config(config)
    step = cfg.resource_map["web"].steps[0]
    assert step.encrypted is True
    assert step.kind == "manifest"


def test_loader_rejects_encrypted_without_manifests(tmp_path):
    from kflow.loader import load_root_config
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "web.yaml").write_text(
        "kflow:\n  version: v1\n  kind: ResourceDefinition\n"
        "name: web\nnamespace: apps\n"
        "steps:\n  - name: bad\n    encrypted: true\n    secret:\n      literals:\n        k: v\n"
    )
    config = proj / "kflow.yaml"
    config.write_text(
        "kflow:\n  version: v1\n  kind: Config\n"
        "resources:\n  - web.yaml\n"
    )
    with pytest.raises(ConfigError):
        load_root_config(config)


# --------------------------------------------------------------------------- #
# Engine integration (decryption + stdin apply)
# --------------------------------------------------------------------------- #


def test_engine_applies_decrypted_manifest_via_stdin(tmp_path, monkeypatch, recorder):
    from kflow.engine import Kflow
    config, key = _write_project(tmp_path)
    monkeypatch.setenv("KFLOW_KEY", key)
    engine = Kflow.load(str(config))
    engine.apply(["web"], wait=False)
    # The decrypted YAML must be piped over stdin, never written to a temp file.
    stdin_payloads = [c["input"] for c in recorder if c["input"]]
    assert any("db-credentials" in (p or "") for p in stdin_payloads)
    # And no kubectl command should reference the .enc path with -f <file>.
    for c in recorder:
        if "apply" in c["cmd"]:
            assert "secret.yaml.enc" not in " ".join(c["cmd"])


def test_engine_destroy_decrypts_via_stdin(tmp_path, monkeypatch, recorder):
    from kflow.engine import Kflow
    config, key = _write_project(tmp_path)
    monkeypatch.setenv("KFLOW_KEY", key)
    engine = Kflow.load(str(config))
    engine.destroy(["web"])
    deletes = [c for c in recorder if "delete" in c["cmd"] and c["input"]]
    assert any("db-credentials" in (c["input"] or "") for c in deletes)


def test_engine_missing_key_raises(tmp_path, monkeypatch, recorder):
    from kflow.engine import Kflow
    from kflow.models import KflowError
    config, key = _write_project(tmp_path)
    # Remove the .env key source and the env var entirely.
    (config.parent / ".env").unlink()
    monkeypatch.delenv("KFLOW_KEY", raising=False)
    engine = Kflow.load(str(config))
    with pytest.raises(KflowError):
        engine.apply(["web"], wait=False)


def test_engine_unencrypted_still_uses_file_path(tmp_path, monkeypatch, recorder):
    from kflow.engine import Kflow
    config, key = _write_project(tmp_path, encrypted=False)
    monkeypatch.setenv("KFLOW_KEY", key)
    engine = Kflow.load(str(config))
    engine.apply(["web"], wait=False)
    # Without the flag the .enc file is passed verbatim to kubectl -f.
    assert any("secret.yaml.enc" in " ".join(c["cmd"]) for c in recorder)


def test_engine_encryption_key_id_override(tmp_path, monkeypatch, recorder):
    from kflow.engine import Kflow
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "state").mkdir()
    prod_key = crypto.generate_key()
    monkeypatch.setenv("KFLOW_KEY_PROD", prod_key)
    monkeypatch.delenv("KFLOW_KEY", raising=False)
    (proj / "secret.yaml.enc").write_text(
        crypto.encrypt_bytes(SECRET_YAML.encode(), prod_key, kid="prod")
    )
    (proj / "web.yaml").write_text(
        "kflow:\n  version: v1\n  kind: ResourceDefinition\n"
        "name: web\nnamespace: apps\nphase: app\n"
        "steps:\n  - name: secret\n    encrypted: true\n"
        "    encryptionKeyId: prod\n"
        "    manifests:\n      - secret.yaml.enc\n"
    )
    config = proj / "kflow.yaml"
    config.write_text(
        "kflow:\n  version: v1\n  kind: Config\n"
        f"state:\n  dir: {proj / 'state'}\n"
        "phases:\n  - name: app\n"
        "resources:\n  - web.yaml\n"
    )
    engine = Kflow.load(str(config))
    engine.apply(["web"], wait=False)
    assert any("db-credentials" in (c["input"] or "") for c in recorder)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_keygen_prints_key():
    result = CliRunner().invoke(cli, ["crypto", "keygen"])
    assert result.exit_code == 0
    first = result.output.splitlines()[0]
    assert first.startswith("KFLOW_KEY=")
    key = first.split("=", 1)[1].strip()
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_cli_keygen_with_id():
    result = CliRunner().invoke(cli, ["crypto", "keygen", "--id", "prod"])
    assert result.exit_code == 0
    assert result.output.startswith("KFLOW_KEY_PROD=")


def test_cli_keygen_passphrase_is_deterministic():
    runner = CliRunner()
    a = runner.invoke(cli, ["crypto", "keygen", "--passphrase", "secret-phrase"])
    b = runner.invoke(cli, ["crypto", "keygen", "--passphrase", "secret-phrase"])
    assert a.output == b.output


def test_cli_keygen_env_writes_file(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["crypto", "keygen", "--env"])
        assert result.exit_code == 0
        from pathlib import Path
        env_text = Path(".env").read_text()
        assert "KFLOW_KEY=" in env_text
        # second time without --force should fail
        again = runner.invoke(cli, ["crypto", "keygen", "--env"])
        assert again.exit_code != 0
        # with --force succeeds
        forced = runner.invoke(cli, ["crypto", "keygen", "--env", "--force"])
        assert forced.exit_code == 0


def test_cli_encrypt_decrypt_round_trip(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        from pathlib import Path
        key = crypto.generate_key()
        Path(".env").write_text(f"KFLOW_KEY={key}\n")
        Path("secret.yaml").write_text(SECRET_YAML)
        enc = runner.invoke(cli, ["crypto", "encrypt", "secret.yaml"])
        assert enc.exit_code == 0, enc.output
        assert Path("secret.yaml.enc").exists()
        assert crypto.is_encrypted_file("secret.yaml.enc")
        dec = runner.invoke(cli, ["crypto", "decrypt", "secret.yaml.enc"])
        assert dec.exit_code == 0
        assert "hunter2" in dec.output


def test_cli_encrypt_refuses_overwrite_without_force(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        from pathlib import Path
        Path(".env").write_text(f"KFLOW_KEY={crypto.generate_key()}\n")
        Path("secret.yaml").write_text(SECRET_YAML)
        Path("secret.yaml.enc").write_text("existing")
        result = runner.invoke(cli, ["crypto", "encrypt", "secret.yaml"])
        assert result.exit_code != 0


def test_cli_info_needs_no_key(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        from pathlib import Path
        key = crypto.generate_key()
        Path("s.enc").write_text(crypto.encrypt_bytes(b"data", key, kid="prod", name="s.yaml"))
        # No .env / no key available, info must still work.
        result = runner.invoke(cli, ["crypto", "info", "s.enc"])
        assert result.exit_code == 0
        assert "prod" in result.output
        assert "fernet" in result.output


def test_cli_keys_lists_fingerprints(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        from pathlib import Path
        key = crypto.generate_key()
        Path(".env").write_text(f"KFLOW_KEY={key}\n")
        result = runner.invoke(cli, ["crypto", "keys"])
        assert result.exit_code == 0
        assert crypto.key_fingerprint(key) in result.output


def test_cli_rekey_rotates(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        from pathlib import Path
        old, new = crypto.generate_key(), crypto.generate_key()
        Path(".env").write_text(f"KFLOW_KEY={old}\nKFLOW_KEY_NEW={new}\n")
        Path("s.enc").write_text(crypto.encrypt_bytes(SECRET_YAML.encode(), old, kid="default"))
        result = runner.invoke(cli, ["crypto", "rekey", "s.enc", "--to", "new"])
        assert result.exit_code == 0, result.output
        env = crypto.Envelope.loads(Path("s.enc").read_text())
        assert env.kid == "new"
        # Decryptable with the new key only.
        assert crypto.decrypt_bytes(Path("s.enc").read_text(), new).decode() == SECRET_YAML


def test_cli_verify_ok(tmp_path, recorder):
    config, key = _write_project(tmp_path)
    runner = CliRunner()
    import os
    os.environ["KFLOW_KEY"] = key
    try:
        result = runner.invoke(cli, ["-c", str(config), "crypto", "verify"])
        assert result.exit_code == 0, result.output
        assert "decrypt cleanly" in result.output
    finally:
        del os.environ["KFLOW_KEY"]


def test_cli_verify_detects_bad_key(tmp_path, recorder, monkeypatch):
    config, key = _write_project(tmp_path)
    # Replace the .env key with an unrelated one.
    (config.parent / ".env").write_text(f"KFLOW_KEY={crypto.generate_key()}\n")
    monkeypatch.delenv("KFLOW_KEY", raising=False)
    result = CliRunner().invoke(cli, ["-c", str(config), "crypto", "verify"])
    assert result.exit_code != 0
