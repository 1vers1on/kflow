"""Manifest encryption: key management, the encrypted-file envelope, and the
key ring that resolves keys from the environment and ``.env`` files.

The goal is to let you commit potentially sensitive manifests (Secrets, certs,
tokens baked into a ConfigMap, …) to a public or shared git repository while
keeping the plaintext private. You encrypt a manifest ahead of time with
:func:`encrypt_bytes` (or ``kflow crypto encrypt``); kflow decrypts it *in
memory* at apply time and pipes the plaintext straight to ``kubectl`` over
stdin, so the cleartext never touches disk.

Design notes
------------
* The symmetric primitive is Fernet (AES-128-CBC + HMAC-SHA256) from the
  ``cryptography`` package. Fernet tokens are authenticated, so a tampered or
  truncated ciphertext fails loudly rather than decrypting to garbage.
* Keys are 32-byte url-safe-base64 strings (the Fernet key format). They can be
  generated randomly (:func:`generate_key`) or derived deterministically from a
  passphrase with scrypt (:func:`derive_key`) so a human can regenerate the same
  key on another machine from a memorised phrase + salt.
* A *key ring* can hold several keys identified by a short ``kid`` (key id).
  Every encrypted file records the ``kid`` it was sealed with, which makes key
  rotation (:func:`KeyRing` + ``kflow crypto rekey``) and decrypting a mixed bag
  of files trivial: kflow picks the right key automatically and, failing that,
  tries every key it knows.
* The on-disk format is a small, human-inspectable text envelope (see
  :class:`Envelope`) so ``git diff`` shows *something*, and so metadata (which
  key, when, original filename) can be read with ``kflow crypto info`` without
  possessing any key.

The ``cryptography`` dependency is imported lazily: importing :mod:`kflow` does
not require it, and only the encryption code paths fail (with a helpful message)
if it is somehow missing.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .models import KflowError

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

#: Marker line that starts every encrypted file. Used to recognise an envelope
#: without attempting to decrypt it.
MAGIC = "$KFLOW-ENCRYPTED$"

#: Separator between the header block and the base64 payload.
SEPARATOR = "---"

#: Current envelope format version.
ENVELOPE_VERSION = 1

#: The only algorithm currently supported.
ALG_FERNET = "fernet"

#: Default key id, used when no explicit ``kid`` is requested and for the
#: ``KFLOW_KEY`` environment variable.
DEFAULT_KID = "default"

#: Environment-variable conventions for keys.
ENV_KEY_DEFAULT = "KFLOW_KEY"
ENV_KEY_PREFIX = "KFLOW_KEY_"  # KFLOW_KEY_<ID>  ->  kid "<id>" (lower-cased)
ENV_FILE_VAR = "KFLOW_ENV_FILE"  # explicit override for the .env path

#: Default salt for passphrase-derived keys. Deterministic on purpose so the
#: same passphrase yields the same key without having to remember a salt; pass
#: ``--salt`` (or the ``salt`` argument) for a stronger, project-specific value.
DEFAULT_SALT = b"kflow-static-salt-v1"

# scrypt cost parameters (RFC 7914). Tuned for interactive use.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EncryptionError(KflowError):
    """Raised for any encryption/decryption or key-management failure."""


# --------------------------------------------------------------------------- #
# Lazy ``cryptography`` import
# --------------------------------------------------------------------------- #


def _fernet_module():
    """Import and return ``cryptography.fernet``; raise a friendly error if the
    optional dependency is not installed."""
    try:
        from cryptography import fernet  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only without the dep
        raise EncryptionError(
            "manifest encryption requires the 'cryptography' package.\n"
            "Install it with:  pip install cryptography   (or  pip install kflow-py[crypto])"
        ) from exc
    return fernet


# --------------------------------------------------------------------------- #
# Key material
# --------------------------------------------------------------------------- #


def generate_key() -> str:
    """Return a fresh random encryption key (Fernet format, url-safe base64)."""
    return _fernet_module().Fernet.generate_key().decode("ascii")


def derive_key(passphrase: str, *, salt: Optional[bytes] = None) -> str:
    """Derive a deterministic Fernet key from ``passphrase`` using scrypt.

    The same ``passphrase`` and ``salt`` always yield the same key, so a key can
    be reconstructed from memory. Use a unique ``salt`` per project for real
    secrets; the default salt only protects against cross-tool rainbow tables.
    """
    if not passphrase:
        raise EncryptionError("passphrase must not be empty")
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise EncryptionError(
            "passphrase-derived keys require the 'cryptography' package."
        ) from exc
    kdf = Scrypt(salt=salt or DEFAULT_SALT, length=32,
                 n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw).decode("ascii")


def normalize_key(key: str) -> str:
    """Validate and normalise a key string, returning the canonical form.

    Accepts either a 44-char url-safe-base64 Fernet key or a raw 32-byte key in
    standard/url-safe base64; raises :class:`EncryptionError` if it cannot be
    coerced into a valid Fernet key.
    """
    key = (key or "").strip()
    if not key:
        raise EncryptionError("empty encryption key")
    candidate = key
    # Allow standard base64 too, by translating to the url-safe alphabet.
    candidate = candidate.replace("+", "-").replace("/", "_")
    try:
        raw = base64.urlsafe_b64decode(_pad_b64(candidate))
    except Exception as exc:
        raise EncryptionError(f"key is not valid base64: {exc}") from exc
    if len(raw) != 32:
        raise EncryptionError(
            f"key must decode to 32 bytes (got {len(raw)}). "
            "Generate one with 'kflow crypto keygen'."
        )
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _pad_b64(value: str) -> str:
    return value + "=" * (-len(value) % 4)


def key_fingerprint(key: str) -> str:
    """A short, non-reversible fingerprint of a key, for display/verification."""
    import hashlib
    raw = base64.urlsafe_b64decode(_pad_b64(normalize_key(key)))
    return hashlib.sha256(raw).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


@dataclass
class Envelope:
    """The parsed contents of an encrypted file.

    ``token`` is the Fernet ciphertext (url-safe base64). The remaining fields
    are clear-text metadata stored in the header so a file can be inspected
    without any key.
    """

    token: str
    kid: str = DEFAULT_KID
    alg: str = ALG_FERNET
    version: int = ENVELOPE_VERSION
    created: str = ""
    name: str = ""  # original basename, optional

    def dumps(self) -> str:
        """Serialise to the on-disk envelope text."""
        lines = [MAGIC, f"version: {self.version}", f"alg: {self.alg}",
                 f"kid: {self.kid}"]
        if self.created:
            lines.append(f"created: {self.created}")
        if self.name:
            lines.append(f"name: {self.name}")
        lines.append(SEPARATOR)
        lines.extend(_wrap(self.token, 64))
        return "\n".join(lines) + "\n"

    @classmethod
    def loads(cls, text: str) -> "Envelope":
        """Parse envelope text; raise :class:`EncryptionError` if malformed."""
        raw_lines = text.splitlines()
        # Skip leading blank lines before the magic marker.
        idx = 0
        while idx < len(raw_lines) and not raw_lines[idx].strip():
            idx += 1
        if idx >= len(raw_lines) or raw_lines[idx].strip() != MAGIC:
            raise EncryptionError(
                "not a kflow-encrypted file (missing magic header). "
                "Encrypt it first with 'kflow crypto encrypt'."
            )
        idx += 1
        header: Dict[str, str] = {}
        while idx < len(raw_lines):
            line = raw_lines[idx]
            idx += 1
            if line.strip() == SEPARATOR:
                break
            if not line.strip():
                continue
            key, sep, value = line.partition(":")
            if not sep:
                raise EncryptionError(f"malformed envelope header line: {line!r}")
            header[key.strip().lower()] = value.strip()
        else:
            raise EncryptionError("envelope is missing the '---' separator")

        token = "".join(ln.strip() for ln in raw_lines[idx:] if ln.strip())
        if not token:
            raise EncryptionError("envelope has an empty payload")

        try:
            version = int(header.get("version", ENVELOPE_VERSION))
        except ValueError as exc:
            raise EncryptionError(f"invalid envelope version: {header.get('version')!r}") from exc
        if version != ENVELOPE_VERSION:
            raise EncryptionError(
                f"unsupported envelope version {version} "
                f"(this kflow understands version {ENVELOPE_VERSION})"
            )
        alg = header.get("alg", ALG_FERNET)
        if alg != ALG_FERNET:
            raise EncryptionError(f"unsupported encryption algorithm: {alg!r}")
        return cls(
            token=token,
            kid=header.get("kid", DEFAULT_KID),
            alg=alg,
            version=version,
            created=header.get("created", ""),
            name=header.get("name", ""),
        )


def is_encrypted(text: str) -> bool:
    """Return True if ``text`` looks like a kflow encrypted envelope."""
    for line in text.splitlines():
        if not line.strip():
            continue
        return line.strip() == MAGIC
    return False


def is_encrypted_file(path) -> bool:
    """Return True if ``path`` exists and begins with the envelope magic."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(len(MAGIC) + 4)
    except OSError:
        return False
    return head.lstrip().startswith(MAGIC)


def _wrap(value: str, width: int) -> List[str]:
    return [value[i:i + width] for i in range(0, len(value), width)] or [""]


# --------------------------------------------------------------------------- #
# Encrypt / decrypt primitives
# --------------------------------------------------------------------------- #


def encrypt_bytes(data: bytes, key: str, *, kid: str = DEFAULT_KID,
                  name: str = "") -> str:
    """Encrypt ``data`` with ``key`` and return the envelope text."""
    fernet = _fernet_module()
    f = fernet.Fernet(normalize_key(key).encode("ascii"))
    token = f.encrypt(data).decode("ascii")
    env = Envelope(
        token=token,
        kid=kid or DEFAULT_KID,
        created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        name=name or "",
    )
    return env.dumps()


def decrypt_bytes(envelope_text: str, key: str) -> bytes:
    """Decrypt an envelope with a single ``key``; raise on failure."""
    env = Envelope.loads(envelope_text)
    return _decrypt_token(env.token, key)


def _decrypt_token(token: str, key: str) -> bytes:
    fernet = _fernet_module()
    f = fernet.Fernet(normalize_key(key).encode("ascii"))
    try:
        return f.decrypt(token.encode("ascii"))
    except fernet.InvalidToken as exc:
        raise EncryptionError(
            "decryption failed: wrong key or corrupted ciphertext"
        ) from exc


# --------------------------------------------------------------------------- #
# .env parsing
# --------------------------------------------------------------------------- #


def parse_dotenv(text: str) -> Dict[str, str]:
    """Parse ``.env``-style text into a dict.

    Supports ``KEY=value``, optional ``export`` prefix, ``#`` comments, blank
    lines, and single/double quoted values. Later assignments win.
    """
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_dotenv_file(path) -> Dict[str, str]:
    """Parse a ``.env`` file; return ``{}`` if it does not exist."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return parse_dotenv(p.read_text(encoding="utf-8"))
    except OSError:
        return {}


# --------------------------------------------------------------------------- #
# Key ring
# --------------------------------------------------------------------------- #


@dataclass
class KeyRing:
    """A collection of named keys used to encrypt and decrypt envelopes."""

    keys: Dict[str, str] = field(default_factory=dict)
    #: Insertion order; the first key added is treated as the primary/default.
    order: List[str] = field(default_factory=list)

    def add(self, kid: str, key: str) -> None:
        kid = (kid or DEFAULT_KID).strip()
        self.keys[kid] = normalize_key(key)
        if kid not in self.order:
            self.order.append(kid)

    def __bool__(self) -> bool:
        return bool(self.keys)

    def __contains__(self, kid: str) -> bool:
        return kid in self.keys

    @property
    def kids(self) -> List[str]:
        return list(self.order)

    @property
    def primary_kid(self) -> Optional[str]:
        if DEFAULT_KID in self.keys:
            return DEFAULT_KID
        return self.order[0] if self.order else None

    def get(self, kid: str) -> Optional[str]:
        return self.keys.get(kid)

    def require(self, kid: str) -> str:
        key = self.keys.get(kid)
        if key is None:
            raise EncryptionError(
                f"no key for id {kid!r} is available.\n"
                f"Set {_env_var_for(kid)} in your environment or .env file "
                f"(known key ids: {self.kids or 'none'})."
            )
        return key

    def encrypt(self, data: bytes, *, kid: Optional[str] = None,
                name: str = "") -> str:
        target = kid or self.primary_kid
        if target is None:
            raise EncryptionError(
                "no encryption keys available; generate one with "
                "'kflow crypto keygen'."
            )
        return encrypt_bytes(data, self.require(target), kid=target, name=name)

    def decrypt(self, envelope_text: str) -> bytes:
        """Decrypt an envelope, selecting the right key by its ``kid`` and
        falling back to trying every key on the ring."""
        env = Envelope.loads(envelope_text)
        if not self.keys:
            raise EncryptionError(
                "no decryption keys available.\n"
                "Set KFLOW_KEY in your environment or a .env file next to the "
                "config (generate one with 'kflow crypto keygen')."
            )
        # 1) exact key id match.
        if env.kid in self.keys:
            return _decrypt_token(env.token, self.keys[env.kid])
        # 2) fall back to trying every key (handles missing/renamed kids).
        for kid in self.order:
            try:
                return _decrypt_token(env.token, self.keys[kid])
            except EncryptionError:
                continue
        raise EncryptionError(
            f"could not decrypt: no key on the ring matches "
            f"(envelope key id {env.kid!r}, available ids {self.kids}). "
            f"Set {_env_var_for(env.kid)} to the correct key."
        )

    # -- construction -----------------------------------------------------

    @classmethod
    def from_mapping(cls, mapping: Dict[str, str]) -> "KeyRing":
        """Build a ring from a flat env-style mapping (``KFLOW_KEY*`` vars)."""
        ring = cls()
        # Default key first so it becomes primary.
        if mapping.get(ENV_KEY_DEFAULT):
            try:
                ring.add(DEFAULT_KID, mapping[ENV_KEY_DEFAULT])
            except EncryptionError:
                pass
        for var, value in mapping.items():
            if not value or not var.startswith(ENV_KEY_PREFIX):
                continue
            kid = var[len(ENV_KEY_PREFIX):].lower()
            if not kid:
                continue
            try:
                ring.add(kid, value)
            except EncryptionError:
                continue
        return ring

    @classmethod
    def from_environment(cls, search_paths: Optional[Iterable] = None) -> "KeyRing":
        """Resolve keys from ``os.environ`` and ``.env`` files.

        Precedence (highest first): real environment variables, then each
        ``.env`` file in ``search_paths`` (earlier paths win), then a file named
        by ``KFLOW_ENV_FILE``. Only ``KFLOW_KEY`` / ``KFLOW_KEY_*`` variables are
        consulted; the process environment is never mutated.
        """
        merged: Dict[str, str] = {}
        # Lowest precedence: explicit env-file override.
        explicit = os.environ.get(ENV_FILE_VAR)
        if explicit:
            merged.update(load_dotenv_file(explicit))
        # Then .env files from the search paths (later paths are lower priority).
        seen: set = set()
        for base in reversed(list(search_paths or [])):
            candidate = Path(base) / ".env"
            resolved = str(candidate.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            merged.update(load_dotenv_file(candidate))
        # Highest precedence: the real environment.
        for var, value in os.environ.items():
            if var == ENV_KEY_DEFAULT or var.startswith(ENV_KEY_PREFIX):
                merged[var] = value
        return cls.from_mapping(merged)


def _env_var_for(kid: str) -> str:
    if not kid or kid == DEFAULT_KID:
        return ENV_KEY_DEFAULT
    return f"{ENV_KEY_PREFIX}{kid.upper()}"


def env_var_for(kid: str) -> str:
    """Public alias: the environment variable name that holds key ``kid``."""
    return _env_var_for(kid)
