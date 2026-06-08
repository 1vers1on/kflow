# Encrypted manifests example

A minimal, runnable project showing how kflow stores a Kubernetes Secret
encrypted in git and decrypts it at apply time.

```
encrypted/
├── kflow.yaml                       # root config
├── app.yaml                         # resource: one encrypted step + one plain step
├── .env.example                     # the DEMO key (copy to .env to run)
└── manifests/
    ├── db-secret.yaml.enc           # encrypted Secret (committed, opaque)
    └── deployment.yaml              # ordinary manifest
```

## Run it

```bash
cd examples/encrypted
cp .env.example .env          # .env is gitignored; holds the demo key

kflow crypto info manifests/db-secret.yaml.enc   # envelope metadata, no key
kflow crypto verify                              # confirm it decrypts
kflow crypto decrypt manifests/db-secret.yaml.enc  # peek at the plaintext
kflow --dry-run apply                            # watch it pipe to kubectl
```

> The key in `.env.example` is a **published demo key** — fine for this example,
> never for real secrets. For your own project, generate a key with
> `kflow crypto keygen --env` and re-encrypt with `kflow crypto encrypt`.

See [../../docs/encryption.md](../../docs/encryption.md) for the full guide.
