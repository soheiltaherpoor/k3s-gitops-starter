# 1. Flux rather than Argo CD

Status: accepted

## Context

Both tools are mature and either would work. The choice shapes everything else: how clusters are described in git, how secrets are decrypted, and what must run in the cluster.

## Decision

Use Flux, installed with only the source and kustomize controllers.

- It has SOPS decryption built in: the Flux `Kustomization` names a secret holding the age key, with no plugin to install or maintain.
- It is small. Two controllers are enough for plain kustomize, which matters on a single k3s node.
- Its resources map directly to directories: one `Kustomization` per path, with explicit `dependsOn` ordering and `wait: true` health checks. That reads well in a review and is easy to test.
- There is no UI to keep authenticated or to secure. Status is `flux get` and `kubectl`.

## Consequences

- No dashboard out of the box. Teams that want one can add Weave GitOps or switch to Argo CD; the `apps/` and `infrastructure/` layout does not depend on Flux.
- Helm and image automation controllers are left out. Add them when a service needs them.
- Argo CD's application-of-applications and per-app sync UI are not available. Ordering here is `infrastructure` before `apps`, nothing finer.
