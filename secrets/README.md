# Secrets

This directory is where encrypted Secret manifests would live, named `*.sops.yaml`. None are committed: the repository ships no keys, and an encrypted file is only useful with a key of your own.

## Add one

```sh
age-keygen -o age.key                  # prints the public key; keep age.key out of git
# put the public key (age1...) into .sops.yaml

kubectl create secret generic db --namespace podinfo \
  --from-literal=password='change-me' --dry-run=client -o yaml > db.sops.yaml
sops --encrypt --in-place db.sops.yaml
mv db.sops.yaml apps/podinfo/overlays/staging/
# list it under resources: in that overlay's kustomization.yaml
```

Give the cluster the private key once, outside git:

```sh
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=age.key
```

Flux decrypts under the `apps` path with that key. `python3 tools/gitops.py check` fails the build if any Secret manifest in the repository is not encrypted.
