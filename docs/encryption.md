# Encrypted manifests

kflow can encrypt Kubernetes manifests so you can commit sensitive material
(Secrets, TLS certs, tokens baked into a ConfigMap, …) to a **public or shared
git repository** and still apply them at runtime — as long as you hold the key.

The plaintext is decrypted **in memory** at apply time and piped straight to
`kubectl` over stdin. The cleartext never touches disk and is never written to a
temp file.

```bash
# 1. make a key (printed to stdout; add it to .env, which is gitignored)
kflow crypto keygen --env

# 2. encrypt a manifest -> secret.yaml.enc  (commit this)
kflow crypto encrypt secret.yaml

# 3. reference the .enc file from a step with `encrypted: true`
#    kflow decrypts it at apply time using the key from .env
kflow apply
```

---

## How it works

* **Cipher.** Encryption uses [Fernet](https://cryptography.io/en/latest/fernet/)
  (AES-128-CBC with an HMAC-SHA256 authentication tag) from the `cryptography`
  package. Because Fernet tokens are authenticated, a tampered or truncated
  ciphertext fails loudly instead of silently decrypting to garbage.
* **Keys** are 32-byte url-safe-base64 strings (the Fernet key format). Generate
  them randomly or derive them deterministically from a passphrase.
* **Key ids (`kid`).** A key ring can hold several keys, each identified by a
  short id. Every encrypted file records which key id sealed it, so kflow can
  pick the right key automatically and key rotation is painless.
* **The `.env` file.** At apply time kflow looks for keys in the environment and
  in `.env` files next to the config and in the working directory. `.env` is
  gitignored by the default kflow `.gitignore`, so the key stays off git while
  the encrypted manifests go on git.

### The envelope format

An encrypted file is a small, human-inspectable text envelope. You can read its
metadata with `kflow crypto info` **without holding any key**:

```
$KFLOW-ENCRYPTED$
version: 1
alg: fernet
kid: default
created: 2026-06-08T19:57:12Z
name: secret.yaml
---
gAAAAABqJx6YI8_IjhXygjD5Nn4FlBZTaa_FxhGMnknh0xaza4d4JPhFVaIb-y2n
HUvi44H_hfl6ZZKw75Qxuon_5UuhtovbWWFj9OI2oIi1CMz_w6--XzM=
```

The header block (everything before `---`) is cleartext metadata. The payload is
the Fernet ciphertext. None of the metadata reveals the plaintext.

---

## Keys

### Where keys come from

When kflow needs to decrypt, it builds a **key ring** by merging, in order of
increasing precedence:

1. A file named by the `KFLOW_ENV_FILE` environment variable (if set).
2. `.env` files found next to the root config and in the current directory.
3. Real environment variables.

Only these variables are consulted (your process environment is never mutated):

| Variable | Key id |
| --- | --- |
| `KFLOW_KEY` | `default` |
| `KFLOW_KEY_<ID>` | `<id>` (lower-cased) — e.g. `KFLOW_KEY_PROD` → `prod` |

A real environment variable always wins over the same variable in a `.env` file,
which lets CI inject keys without editing files.

### Generating keys

```bash
kflow crypto keygen                 # random default key, printed to stdout
kflow crypto keygen --id prod       # random key for id "prod" (KFLOW_KEY_PROD=)
kflow crypto keygen --env           # append KFLOW_KEY=... to ./.env
kflow crypto keygen --id prod --env # append KFLOW_KEY_PROD=... to ./.env
```

`--env` refuses to clobber an existing entry unless you pass `--force`.

#### Passphrase-derived keys

A key can be derived **deterministically** from a passphrase using scrypt, so you
can reconstruct it from memory on another machine — no key file to copy around:

```bash
kflow crypto keygen --passphrase "correct horse battery staple"
kflow crypto keygen --passphrase "…" --salt my-project-salt   # recommended
```

The same passphrase + salt always produce the same key. Use a unique `--salt`
per project; the default salt only guards against generic rainbow tables.

### Listing keys

```bash
kflow crypto keys
```

```
                 encryption keys
  key id    env var          fingerprint    primary
 ─────────────────────────────────────────────────────
  default   KFLOW_KEY        036eff2989b6   ✓
  prod      KFLOW_KEY_PROD   a1b2c3d4e5f6
```

The fingerprint is a short, non-reversible hash of the key — handy for confirming
two machines hold the *same* key without revealing it.

---

## Encrypting and decrypting files

```bash
kflow crypto encrypt secret.yaml                 # -> secret.yaml.enc
kflow crypto encrypt secret.yaml -o sealed.enc   # custom output path
kflow crypto encrypt secret.yaml --id prod       # seal with the "prod" key
kflow crypto encrypt secret.yaml --stdout        # write envelope to stdout
kflow crypto encrypt secret.yaml --in-place      # replace the source file

kflow crypto decrypt secret.yaml.enc             # plaintext to stdout
kflow crypto decrypt secret.yaml.enc -o out.yaml # plaintext to a file

kflow crypto info secret.yaml.enc                # metadata only, no key needed
```

If you omit `--id`, encryption uses the **primary** key — `default` if present,
otherwise the first key on the ring.

---

## Using encrypted manifests in a workflow

Set `encrypted: true` on a `manifests` step and point it at your `.enc` files:

```yaml
kflow:
  version: v1
  kind: ResourceDefinition

name: app
namespace: demo
phase: apps

steps:
  - name: db-credentials
    encrypted: true
    manifests:
      - manifests/db-secret.yaml.enc

  - name: deploy
    dependsOn:
      - db-credentials
    manifests:
      - manifests/app-deployment.yaml   # ordinary, unencrypted
```

Behaviour:

* **apply / reload** — each `.enc` file is decrypted in memory and applied via
  `kubectl apply -f -` (stdin). With `--dry-run` the file is still decrypted (to
  validate the key) but the `kubectl` call is skipped.
* **destroy** — the decrypted manifest is piped to `kubectl delete -f -`.
* The decrypted bytes are **never written to disk** and never appear in a
  command line.

`encrypted: true` is only valid on manifest steps; the loader rejects it
elsewhere.

### Choosing a specific key (`encryptionKeyId`)

Normally the key id is read from the envelope and the matching key is selected
automatically. To force a particular key regardless of what the envelope records:

```yaml
- name: db-credentials
  encrypted: true
  encryptionKeyId: prod
  manifests:
    - manifests/db-secret.yaml.enc
```

### Verifying before you deploy

`kflow crypto verify` walks the config, finds every `encrypted: true` manifest,
and checks that each one decrypts with an available key — without contacting the
cluster. Great for a CI gate:

```bash
kflow crypto verify
```

```
            encrypted manifests
  resource   step             manifest             key id    status
 ──────────────────────────────────────────────────────────────────
  app        db-credentials   db-secret.yaml.enc   default   ok

✓ all 1 encrypted manifest(s) decrypt cleanly
```

It exits non-zero if any manifest cannot be decrypted.

---

## Key rotation

Add a new key, re-encrypt the affected files with it, then retire the old key:

```bash
kflow crypto keygen --id v2 --env          # add KFLOW_KEY_V2 to .env
kflow crypto rekey manifests/db.yaml.enc --to v2
kflow crypto verify                        # confirm everything still decrypts
# ...then remove the old KFLOW_KEY once nothing references it
```

`kflow crypto rekey` decrypts with whatever key on the ring works and
re-encrypts with the target key (defaulting to the primary key if `--to` is
omitted). Because the key ring **falls back to trying every key it holds**, a
repository mid-rotation — some files on the old key, some on the new — keeps
working as long as both keys are present.

---

## Security notes

* **Keep keys out of git.** The default kflow `.gitignore` excludes `.env`. Never
  commit a key. If a key leaks, rotate it (above) and rebuild affected Secrets.
* **Fernet is symmetric.** Anyone with the key can decrypt. This protects
  manifests *at rest in your repo*, not from someone who already has the key or
  cluster access.
* **Encrypt whole files.** kflow encrypts the entire manifest, not individual
  fields. If a file mixes secret and non-secret content, the whole thing becomes
  opaque in git diffs — split it if you want readable diffs for the public parts.
* **Remote URLs cannot be encrypted manifests.** Encryption applies to local
  files only; an `encrypted` step pointing at an `http(s)://` URL is an error.
* **Metadata is visible.** The envelope header (key id, timestamp, original
  filename) is cleartext by design. Omit the filename if it is itself sensitive
  by re-encrypting from stdin.

---

## Library API

Everything is importable from `kflow` for use in custom runners or scripts:

```python
from kflow import (
    generate_key, derive_key,
    encrypt_bytes, decrypt_bytes,
    Envelope, KeyRing, EncryptionError,
)

key = generate_key()
blob = encrypt_bytes(b"apiVersion: v1\nkind: Secret\n", key, name="s.yaml")
plaintext = decrypt_bytes(blob, key)

ring = KeyRing.from_environment()      # reads KFLOW_KEY* from env + .env
plaintext = ring.decrypt(blob)         # auto-selects the right key
```

See [`kflow/crypto.py`](../kflow/crypto.py) for the full surface.
