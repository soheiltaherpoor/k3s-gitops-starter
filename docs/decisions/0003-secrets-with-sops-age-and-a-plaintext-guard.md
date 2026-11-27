# 3. Secrets with SOPS and age, plus a plaintext guard

Status: accepted

## Context

GitOps wants everything in git, and Kubernetes Secrets are only base64. Options include sealed secrets, an external secrets operator talking to a vault, and encrypting files in the repository. The first two need more moving parts in the cluster. Whatever is chosen, the common failure is not weak crypto but a plaintext Secret committed by mistake.

## Decision

Encrypt files with SOPS using age keys, and let Flux decrypt them. Only `data` and `stringData` values are encrypted, so reviewers still see which keys changed. The private key never enters the repository; it is created by the operator and given to the cluster once.

A checker, `tools/gitops.py check`, runs in CI and fails if any Secret with values is not encrypted, or if a file named `*.sops.yaml` carries no sops metadata or no encrypted values. A test fails if private key material appears anywhere in the repository.

## Consequences

- No extra controller or external service is needed, which suits a small cluster or an air-gapped one.
- Anyone who holds the age private key can read every secret encrypted to it. Key rotation means re-encrypting files; the `sops` tool supports it, and the process is not automated here.
- The end-to-end test uses a throwaway key and encrypts nothing in the repository, so decryption of real committed secrets inside the cluster is not tested; a separate job covers the encrypt, check and decrypt round trip outside it.
- Teams with a vault already should prefer an external secrets operator. The plaintext guard is still worth keeping.
