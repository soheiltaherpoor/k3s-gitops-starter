PYTHON ?= python3
ENV ?= staging
K8S_VERSION ?= 1.31.0

RENDER_DIRS = infrastructure/staging infrastructure/production apps/staging apps/production clusters/staging clusters/production
CRD_CATALOG = https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json

.PHONY: help test check promote promote-write validate sops-test e2e cluster-delete

help:
	@echo "test           Python unit tests (no cluster needed)"
	@echo "check          verify repository invariants"
	@echo "promote        show what promoting staging to production would change"
	@echo "promote-write  copy staging image tags into the production overlay"
	@echo "validate       render every overlay with kubectl and check it with kubeconform"
	@echo "sops-test      secret workflow with a throwaway age key (needs age and sops)"
	@echo "e2e            full test on a local k3d cluster: make e2e ENV=staging|production"
	@echo "cluster-delete remove the cluster left by KEEP_CLUSTER=1 make e2e"

test:
	$(PYTHON) -m unittest discover -s tests -v

check:
	$(PYTHON) tools/gitops.py check

promote:
	$(PYTHON) tools/gitops.py promote

promote-write:
	$(PYTHON) tools/gitops.py promote --write
	$(PYTHON) tools/gitops.py check

validate:
	@mkdir -p .build
	@for dir in $(RENDER_DIRS); do \
		echo "kubectl kustomize $$dir"; \
		kubectl kustomize $$dir > .build/$$(echo $$dir | tr / -).yaml || exit 1; \
	done
	kubeconform -strict -summary -ignore-missing-schemas -kubernetes-version $(K8S_VERSION) \
		-schema-location default -schema-location '$(CRD_CATALOG)' .build/*.yaml

sops-test:
	bash tests/sops-roundtrip.sh

e2e:
	bash tests/e2e.sh $(ENV)

cluster-delete:
	k3d cluster delete gitops-$(ENV)
