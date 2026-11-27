# k3s-gitops-starter

[![CI](https://github.com/soheiltaherpoor/k3s-gitops-starter/actions/workflows/ci.yml/badge.svg)](https://github.com/soheiltaherpoor/k3s-gitops-starter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Flux GitOps starter for k3s. Staging and production are kustomize overlays of one app, promotion is copying an image tag, secrets are encrypted with SOPS and age, and CI boots a real k3d cluster for each environment to prove that it converges and puts back manual changes.

## Why

Most GitOps examples stop at "Flux applied my YAML". The parts that hurt later are the boring ones: staging and production drifting apart, someone bumping production to a version that never ran in staging, a Secret committed in plaintext at 2 a.m., a cluster that quietly differs from git. This starter makes those cases fail in CI instead of in production.

## How it fits together

```mermaid
flowchart LR
    dev([you]) -->|pull request| repo[(git: main)]
    repo -->|tools/gitops.py check| ci{{CI}}
    repo -. clone every minute .-> src[Flux source-controller]
    src --> kc[Flux kustomize-controller]
    kc -->|infrastructure| ns[namespace, quota, Pod Security]
    kc -->|apps, after infrastructure| app[podinfo: staging or production overlay]
    age[(age key in the cluster)] -. decrypts *.sops.yaml .-> kc
```

Two clusters, one repository. Each cluster applies its own `clusters/<env>/sync.yaml` once; after that it follows git.

```text
clusters/<env>/            what this cluster pulls: a GitRepository and two Flux Kustomizations
infrastructure/<env>/      namespace with Pod Security "restricted", resource quota
apps/<env>/                the list of apps for that environment
apps/podinfo/base/         deployment and service, shared
apps/podinfo/overlays/     staging and production: image tag, replicas, settings, PodDisruptionBudget
secrets/                   how to add an encrypted Secret (none are committed)
tools/gitops.py            repository checks and the promotion command
tests/                     unit tests, SOPS round trip, end-to-end test on k3d
```

## Try it locally

Needs Docker, [k3d](https://k3d.io), kubectl, the [Flux CLI](https://fluxcd.io/flux/installation/), `age` and Python 3 with PyYAML.

```sh
pip install -r requirements.txt
make e2e ENV=staging        # about three minutes; also try ENV=production
```

The test creates a k3d cluster, installs Flux, applies `clusters/staging`, waits for the sync, then checks that the running image, replica count, Pod Security level and PodDisruptionBudget match the overlay, that the app reports the expected version, and that `kubectl scale` is reverted by Flux. It deletes the cluster afterwards; `KEEP_CLUSTER=1 make e2e` keeps it for you to poke at, and `make cluster-delete` removes it.

By default Flux clones the repository named in `clusters/*/sync.yaml`, which is this one. To see your own changes reach the cluster, fork the repository, change that URL, push, and run the steps by hand:

```sh
k3d cluster create gitops-staging
flux install --components=source-controller,kustomize-controller
age-keygen -o age.key
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=age.key
kubectl apply -k clusters/staging
flux get kustomizations --watch
```

## Promotion

Each overlay pins the image version it runs, in its `images:` entry. Promotion is copying staging's tag to production:

```sh
make promote          # dry run: shows what would change
make promote-write    # edits the production overlay, then runs the checks
```

You then open a pull request with that one-line change. That review is the production gate. `tools/gitops.py check` runs in CI and fails when:

- an overlay uses `latest`, a tag that is not a semantic version, or no pinned image at all;
- production would run a newer version than staging, so nothing reaches production that staging has not run first;
- a kustomization points at a file or directory that does not exist;
- a cluster's sync points at the wrong environment, a missing path, an unknown source or dependency, or the two clusters disagree about the repository URL;
- a Kubernetes Secret is committed in plaintext, or a `*.sops.yaml` file is not actually encrypted;
- a Namespace has no Pod Security enforcement label, or a base manifest uses an unpinned image.

## Secrets

Secrets live in git encrypted with [SOPS](https://github.com/getsops/sops) and an [age](https://github.com/FiloSottile/age) key. Only the `data` and `stringData` values are encrypted, so diffs stay readable. The private key is created by you, kept out of git, and handed to the cluster once as the `sops-age` secret; Flux decrypts under the `apps` path. The steps are in [secrets/README.md](secrets/README.md). This repository ships no keys and no encrypted secrets, and a test fails if a private key ever lands in it.

## What CI proves

| Job | What it checks |
| --- | --- |
| `test` | Python 3.9, 3.12 and 3.13: the checker's unit tests and the repository's own invariants |
| `validate` | yamllint, ShellCheck, every overlay rendered with `kubectl kustomize` and validated with kubeconform (including the Flux resources) |
| `sops` | With a throwaway age key: a plaintext Secret is rejected, the same file encrypted with sops is accepted, only the key holder can decrypt |
| `e2e` | For staging and production: a real k3d cluster converges on the commit under test, matches its overlay, and corrects a manual `scale` |

The `e2e` job runs on pushes to `main` and manual runs, not on pull requests, because Flux can only clone commits that exist on GitHub.

## Limitations

- **One demo app.** [podinfo](https://github.com/stefanprodan/podinfo) stands in for your services. The checker and `promote` handle any number of apps laid out as `apps/<app>/overlays/<env>`.
- **No ingress, TLS, network policies or monitoring.** They depend on your environment. For alerting on what you deploy, see [slo-observability-pack](https://github.com/soheiltaherpoor/slo-observability-pack).
- **Promotion compares version numbers; it does not know staging was healthy.** The rule stops skipping staging, not shipping a bad release that staging also ran. Add a health or test gate before merging the promotion pull request.
- **Decryption of committed secrets is not exercised in a cluster.** The end-to-end test gives Flux a throwaway key and nothing is encrypted in the repository, so it proves the wiring is accepted, not that a real secret decrypts. The `sops` job covers encrypting, checking and decrypting outside the cluster.
- **Flux is installed with `flux install`, not `flux bootstrap`.** Flux does not manage itself here. For a real cluster use `flux bootstrap`, which commits the Flux components into the repository.
- **Both clusters follow `main`.** Production is protected by reviewing the tag change, not by a separate branch ([ADR 2](docs/decisions/0002-environments-as-overlays-on-one-branch.md)).
- **The Flux CLI in CI is not version-pinned**, so a Flux release can change behaviour. The k3d version, kubeconform and sops are pinned.
- **Single-node clusters.** Nothing here shows high availability of the control plane.

## Design decisions

1. [Flux rather than Argo CD](docs/decisions/0001-flux-rather-than-argo-cd.md)
2. [Environments as overlays on one branch](docs/decisions/0002-environments-as-overlays-on-one-branch.md)
3. [Secrets with SOPS and age, plus a plaintext guard](docs/decisions/0003-secrets-with-sops-age-and-a-plaintext-guard.md)

## License

MIT. See [LICENSE](LICENSE). Security notes are in [SECURITY.md](SECURITY.md).
