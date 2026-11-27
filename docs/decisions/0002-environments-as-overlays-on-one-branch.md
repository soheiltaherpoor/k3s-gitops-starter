# 2. Environments as overlays on one branch

Status: accepted

## Context

There are two common ways to model staging and production in git: a branch per environment, or one branch with a directory per environment. Branches make promotion a merge, but they let the environments drift in ways that are hard to see, and merges carry unrelated changes along.

## Decision

One branch, `main`. Each environment is a kustomize overlay over a shared base, and each cluster's `sync.yaml` points at its own directories. What differs between environments is written down in the overlay: image tag, replica count, resources, settings and extras such as the PodDisruptionBudget.

Promotion is a small, reviewed change: copy staging's image tag into the production overlay (`make promote-write`). CI refuses a production tag that is newer than staging's, so a version cannot skip staging, and refuses `latest` or unpinned images, so what runs can be read from git.

## Consequences

- The difference between staging and production is a diff of two files, not a branch comparison.
- Production follows `main` like staging does. Its protection is the review of the tag change, so branch protection and required reviews on `main` are part of the design, not an extra.
- A rollback is a revert of one line. Rolling staging back below production's version fails the check until production is rolled back too, which is the intended nudge.
- Version comparison assumes semantic version tags. Teams that tag by commit hash or date need a different rule.
