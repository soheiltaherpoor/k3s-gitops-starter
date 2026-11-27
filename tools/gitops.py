#!/usr/bin/env python3
"""Checks and promotion helper for this GitOps repository.

  check                 verify the repository's invariants (see below)
  promote               copy image tags from staging to production
  show ENV --app APP    print what an environment's overlay declares

`check` fails on:
  * an overlay image that is not pinned to a version tag or digest, or is "latest"
  * production running a newer version than staging
  * a kustomization that points at a file or directory that does not exist
  * a Flux sync that points at the wrong environment, a missing path or a missing source
  * a plaintext Kubernetes Secret, or a *.sops.yaml file that is not encrypted
  * a Namespace without a Pod Security enforcement label

Exit codes: 0 ok, 1 problems found, 2 usage error or unreadable files.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only without PyYAML
    sys.exit("gitops.py needs PyYAML: pip install -r requirements.txt")

ENVS = ("staging", "production")  # promotion order: left feeds right
SKIP_DIRS = {".git", ".github", ".build", "node_modules", "__pycache__"}
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:(-[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
PSA_ENFORCE = "pod-security.kubernetes.io/enforce"


class RepoError(Exception):
    """The repository cannot be read as expected."""


def read_yaml_docs(path: Path) -> List[dict]:
    try:
        docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise RepoError("%s: cannot read as YAML: %s" % (path, exc))
    return [d for d in docs if isinstance(d, dict)]


def yaml_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".yaml", ".yml") or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def parse_version(tag: str) -> Optional[Tuple[int, int, int, int]]:
    """Comparable tuple, or None if the tag is not a semantic version.

    A release sorts above its own pre-releases (1.2.3 > 1.2.3-rc1).
    """
    match = SEMVER_RE.match(tag)
    if not match:
        return None
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), 0 if pre else 1)


def rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# --------------------------------------------------------------------------- overlays

def overlay_path(root: Path, app: str, env: str) -> Path:
    return root / "apps" / app / "overlays" / env / "kustomization.yaml"


def discover_apps(root: Path) -> List[str]:
    apps_dir = root / "apps"
    if not apps_dir.is_dir():
        return []
    return sorted(p.name for p in apps_dir.iterdir() if (p / "overlays").is_dir())


def load_overlay(root: Path, app: str, env: str) -> dict:
    path = overlay_path(root, app, env)
    docs = read_yaml_docs(path)
    if not docs:
        raise RepoError("%s: empty or not a mapping" % rel(root, path))
    return docs[0]


def overlay_images(overlay: dict) -> Dict[str, dict]:
    return {i["name"]: i for i in overlay.get("images", []) if isinstance(i, dict) and "name" in i}


def check_overlays(root: Path) -> List[str]:
    problems: List[str] = []
    for app in discover_apps(root):
        loaded: Dict[str, Dict[str, dict]] = {}
        for env in ENVS:
            path = overlay_path(root, app, env)
            if not path.exists():
                problems.append("apps/%s/overlays/%s/kustomization.yaml is missing" % (app, env))
                continue
            where = rel(root, path)
            images = overlay_images(load_overlay(root, app, env))
            if not images:
                problems.append("%s: no images: entry, so the version is not pinned here" % where)
            for name, image in images.items():
                tag = str(image.get("newTag", ""))
                if not tag and not image.get("digest"):
                    problems.append("%s: image %s has neither newTag nor digest" % (where, name))
                elif tag == "latest":
                    problems.append("%s: image %s uses the tag 'latest'" % (where, name))
                elif tag and parse_version(tag) is None:
                    problems.append("%s: image %s tag %r is not a semantic version" % (where, name, tag))
            loaded[env] = images
        problems.extend(_check_not_ahead(app, loaded))
    return problems


def _check_not_ahead(app: str, loaded: Dict[str, Dict[str, dict]]) -> List[str]:
    problems: List[str] = []
    for lower, upper in zip(ENVS, ENVS[1:]):
        if lower not in loaded or upper not in loaded:
            continue
        for name, image in loaded[upper].items():
            if name not in loaded[lower]:
                problems.append("apps/%s: %s runs image %s that %s does not" % (app, upper, name, lower))
                continue
            up = parse_version(str(image.get("newTag", "")))
            low = parse_version(str(loaded[lower][name].get("newTag", "")))
            if up and low and up > low:
                problems.append(
                    "apps/%s: %s runs %s %s, newer than %s (%s); promote only what %s has run"
                    % (app, upper, name, image["newTag"], lower, loaded[lower][name]["newTag"], lower))
    return problems


# --------------------------------------------------------------------------- kustomize references

def check_kustomize_references(root: Path) -> List[str]:
    problems: List[str] = []
    for path in yaml_files(root):
        if path.name != "kustomization.yaml":
            continue
        docs = read_yaml_docs(path)
        if not docs or docs[0].get("kind") != "Kustomization" or "kustomize.config.k8s.io" not in str(
                docs[0].get("apiVersion", "")):
            continue
        for entry in docs[0].get("resources", []) or []:
            if "://" in str(entry) or str(entry).startswith("git@"):
                continue
            target = (path.parent / str(entry)).resolve()
            if target.is_dir():
                if not (target / "kustomization.yaml").exists():
                    problems.append("%s: resource %s is a directory without kustomization.yaml"
                                    % (rel(root, path), entry))
            elif not target.exists():
                problems.append("%s: resource %s does not exist" % (rel(root, path), entry))
        for patch in docs[0].get("patches", []) or []:
            if isinstance(patch, dict) and "path" in patch and not (path.parent / patch["path"]).exists():
                problems.append("%s: patch %s does not exist" % (rel(root, path), patch["path"]))
    return problems


# --------------------------------------------------------------------------- flux

def check_flux(root: Path) -> List[str]:
    problems: List[str] = []
    urls: Dict[str, str] = {}
    for env in ENVS:
        path = root / "clusters" / env / "sync.yaml"
        if not path.exists():
            problems.append("clusters/%s/sync.yaml is missing" % env)
            continue
        where = rel(root, path)
        docs = read_yaml_docs(path)
        sources = {d["metadata"]["name"] for d in docs if d.get("kind") == "GitRepository"}
        kustomizations = [d for d in docs if d.get("kind") == "Kustomization"]
        names = {d["metadata"]["name"] for d in kustomizations}
        for doc in docs:
            if doc.get("kind") == "GitRepository":
                urls[env] = str(doc.get("spec", {}).get("url", ""))
        if not sources:
            problems.append("%s: no GitRepository" % where)
        for doc in kustomizations:
            spec = doc.get("spec", {})
            name = doc["metadata"]["name"]
            if spec.get("sourceRef", {}).get("name") not in sources:
                problems.append("%s: Kustomization %s uses a source that is not defined here" % (where, name))
            for dep in spec.get("dependsOn", []) or []:
                if dep.get("name") not in names:
                    problems.append("%s: Kustomization %s depends on unknown %s" % (where, name, dep.get("name")))
            target = str(spec.get("path", "")).lstrip("./")
            if not (root / target / "kustomization.yaml").exists():
                problems.append("%s: Kustomization %s path %s has no kustomization.yaml"
                                % (where, name, spec.get("path")))
            others = [e for e in ENVS if e != env]
            if any(re.search(r"(^|/)%s(/|$)" % e, target) for e in others):
                problems.append("%s: Kustomization %s points at another environment (%s)"
                                % (where, name, spec.get("path")))
    if len(set(urls.values())) > 1:
        problems.append("clusters/*/sync.yaml: GitRepository URLs differ between environments: %s"
                        % ", ".join("%s=%s" % kv for kv in sorted(urls.items())))
    return problems


# --------------------------------------------------------------------------- secrets and namespaces

def check_secrets(root: Path) -> List[str]:
    problems: List[str] = []
    for path in yaml_files(root):
        text = path.read_text(encoding="utf-8")
        docs = read_yaml_docs(path)
        # ".sops.yaml" itself is sops' configuration file, not an encrypted manifest.
        encrypted_name = (re.search(r"\.sops\.ya?ml$", path.name) is not None
                          and path.name not in (".sops.yaml", ".sops.yml"))
        if encrypted_name and not any(isinstance(d.get("sops"), dict) for d in docs):
            problems.append("%s: named *.sops.yaml but has no sops metadata; it is not encrypted"
                            % rel(root, path))
        elif encrypted_name and "ENC[" not in text:
            problems.append("%s: has sops metadata but no encrypted values" % rel(root, path))
        for doc in docs:
            if doc.get("kind") != "Secret" or not (doc.get("data") or doc.get("stringData")):
                continue
            if not isinstance(doc.get("sops"), dict):
                problems.append("%s: Secret %s is in plaintext; encrypt it with sops"
                                % (rel(root, path), doc.get("metadata", {}).get("name", "?")))
                continue
            values = list((doc.get("data") or {}).values()) + list((doc.get("stringData") or {}).values())
            if any(not str(v).startswith("ENC[") for v in values):
                problems.append("%s: Secret %s has values that are not encrypted"
                                % (rel(root, path), doc.get("metadata", {}).get("name", "?")))
    return problems


def check_namespaces(root: Path) -> List[str]:
    problems: List[str] = []
    for path in yaml_files(root):
        for doc in read_yaml_docs(path):
            if doc.get("kind") == "Namespace":
                labels = doc.get("metadata", {}).get("labels") or {}
                if PSA_ENFORCE not in labels:
                    problems.append("%s: Namespace %s has no %s label"
                                    % (rel(root, path), doc["metadata"].get("name"), PSA_ENFORCE))
    return problems


def check_base_images(root: Path) -> List[str]:
    problems: List[str] = []
    for path in yaml_files(root):
        if "base" not in path.relative_to(root).parts:
            continue
        for doc in read_yaml_docs(path):
            if doc.get("kind") not in ("Deployment", "StatefulSet", "DaemonSet"):
                continue
            pod = doc.get("spec", {}).get("template", {}).get("spec", {})
            for container in (pod.get("containers") or []) + (pod.get("initContainers") or []):
                image = str(container.get("image", ""))
                last = image.rsplit("/", 1)[-1]
                if "@sha256:" in image:
                    continue
                if ":" not in last or last.endswith(":latest"):
                    problems.append("%s: container %s uses an unpinned image %r"
                                    % (rel(root, path), container.get("name"), image))
    return problems


def run_checks(root: Path) -> List[str]:
    problems: List[str] = []
    for check in (check_overlays, check_kustomize_references, check_flux, check_secrets,
                  check_namespaces, check_base_images):
        problems.extend(check(root))
    return problems


# --------------------------------------------------------------------------- promote and show

def promote(root: Path, apps: Sequence[str], write: bool) -> List[str]:
    """Copy staging image tags to production. Returns one line per change."""
    changes: List[str] = []
    for app in apps:
        staging = overlay_images(load_overlay(root, app, "staging"))
        target_path = overlay_path(root, app, "production")
        production = overlay_images(load_overlay(root, app, "production"))
        text = target_path.read_text(encoding="utf-8")
        app_changes: List[str] = []
        for name, image in staging.items():
            new_tag = str(image.get("newTag", ""))
            if not new_tag:
                raise RepoError("apps/%s staging image %s has no newTag to promote" % (app, name))
            if name not in production:
                raise RepoError("apps/%s production overlay has no images: entry for %s" % (app, name))
            old_tag = str(production[name].get("newTag", ""))
            if old_tag == new_tag:
                continue
            pattern = re.compile(
                r"(-\s+name:\s*%s\s*\n(?:[ \t]+[^\n]*\n)*?[ \t]+newTag:\s*)(\S+)" % re.escape(name))
            text, count = pattern.subn(lambda m: m.group(1) + new_tag, text, count=1)
            if count != 1:
                raise RepoError("could not find newTag for %s in %s" % (name, rel(root, target_path)))
            app_changes.append("apps/%s: %s %s -> %s" % (app, name, old_tag or "(none)", new_tag))
        if write and app_changes:
            target_path.write_text(text, encoding="utf-8")
        changes.extend(app_changes)
    return changes


def show(root: Path, env: str, app: str) -> dict:
    overlay = load_overlay(root, app, env)
    images = overlay_images(overlay)
    if not images:
        raise RepoError("apps/%s/overlays/%s declares no images" % (app, env))
    name, image = next(iter(images.items()))
    replicas = {r.get("name"): r.get("count") for r in overlay.get("replicas", []) or []}
    return {
        "app": app,
        "env": env,
        "image": name,
        "tag": str(image.get("newTag", "")),
        "replicas": replicas.get(app),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", default=".", help="repository root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify repository invariants")
    p_promote = sub.add_parser("promote", help="copy staging image tags to production")
    p_promote.add_argument("--app", action="append", help="limit to one app (repeatable)")
    p_promote.add_argument("--write", action="store_true", help="edit the files (default: dry run)")
    p_show = sub.add_parser("show", help="print what an overlay declares")
    p_show.add_argument("env", choices=ENVS)
    p_show.add_argument("--app", required=True)
    p_show.add_argument("--field", choices=("app", "env", "image", "tag", "replicas"),
                        help="print one field instead of JSON")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        if args.command == "check":
            problems = run_checks(root)
            for problem in problems:
                print("FAIL %s" % problem)
            if problems:
                print("%d problem(s)" % len(problems), file=sys.stderr)
                return 1
            print("ok: repository invariants hold")
            return 0
        if args.command == "promote":
            apps = args.app or discover_apps(root)
            changes = promote(root, apps, args.write)
            if not changes:
                print("production already runs what staging runs")
                return 0
            for line in changes:
                print(("promoted " if args.write else "would promote ") + line)
            if not args.write:
                print("dry run; add --write to edit the files")
            return 0
        info = show(root, args.env, args.app)
        print(info[args.field] if args.field else json.dumps(info))
        return 0
    except RepoError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
