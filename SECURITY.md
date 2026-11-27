# Security policy

This repository is a starter: Kubernetes manifests, Flux configuration and a small checker. It contains no credentials and, by design, no private keys. The checker (`tools/gitops.py check`) fails the build if a plaintext Kubernetes Secret is committed.

If you find a vulnerability in the checker, or a manifest here that would put a cluster at risk if used as shown, please report it privately through GitHub's "Report a vulnerability" button on the Security tab rather than in a public issue.

Before using this for anything real:

- generate your own age key, keep the private half out of git, and put your public key in `.sops.yaml`;
- replace the repository URL in `clusters/*/sync.yaml` with your own;
- read `docs/decisions/` for what is deliberately left out (ingress, TLS, network policy, multi-tenancy).
