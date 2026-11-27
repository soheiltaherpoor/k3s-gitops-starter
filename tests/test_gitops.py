import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gitops  # noqa: E402

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".build")
STAGING = "apps/podinfo/overlays/staging/kustomization.yaml"
PRODUCTION = "apps/podinfo/overlays/production/kustomization.yaml"

ENCRYPTED_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "db", "namespace": "podinfo"},
    "stringData": {"password": "ENC[AES256_GCM,data:abc,iv:def,tag:ghi,type:str]"},
    "sops": {"age": [{"recipient": "age1example", "enc": "-----BEGIN AGE ENCRYPTED FILE-----"}]},
}


class RepoCase(unittest.TestCase):
    """Each test gets a private copy of the real repository to break."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "repo"
        shutil.copytree(ROOT, self.root, ignore=IGNORE)

    def path(self, name):
        return self.root / name

    def edit(self, name, old, new):
        path = self.path(name)
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, "test setup: %r not found in %s" % (old, name))
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def write(self, name, content):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else yaml.safe_dump(content),
                        encoding="utf-8")

    def problems(self):
        return gitops.run_checks(self.root)

    def assertProblem(self, fragment):
        found = self.problems()
        self.assertTrue(any(fragment in p for p in found),
                        "expected a problem containing %r, got %r" % (fragment, found))


class RealRepositoryTests(RepoCase):
    def test_the_repository_passes_its_own_checks(self):
        self.assertEqual(self.problems(), [])


class VersionTests(unittest.TestCase):
    def test_ordering(self):
        v = gitops.parse_version
        self.assertLess(v("6.7.0"), v("6.7.1"))
        self.assertLess(v("6.9.0"), v("6.10.0"))
        self.assertEqual(v("v1.2.3"), v("1.2.3"))
        self.assertLess(v("1.2.3-rc1"), v("1.2.3"))

    def test_non_versions(self):
        for tag in ("latest", "main", "6.7", "sha-abc123", ""):
            self.assertIsNone(gitops.parse_version(tag), tag)


class OverlayTests(RepoCase):
    def test_latest_is_rejected(self):
        self.edit(STAGING, "newTag: 6.7.1", "newTag: latest")
        self.assertProblem("'latest'")

    def test_non_semver_tag_is_rejected(self):
        self.edit(STAGING, "newTag: 6.7.1", "newTag: main")
        self.assertProblem("not a semantic version")

    def test_overlay_without_images_is_rejected(self):
        self.write(STAGING, {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                             "resources": ["../../base"]})
        self.assertProblem("no images")

    def test_digest_counts_as_pinned(self):
        self.edit(STAGING, "newTag: 6.7.1", "digest: sha256:" + "a" * 64)
        self.assertEqual(self.problems(), [])

    def test_production_may_lag_or_equal_staging(self):
        self.edit(PRODUCTION, "newTag: 6.7.0", "newTag: 6.7.1")
        self.assertEqual(self.problems(), [])
        self.edit(STAGING, "newTag: 6.7.1", "newTag: 6.8.0")
        self.assertEqual(self.problems(), [])

    def test_production_ahead_of_staging_is_rejected(self):
        self.edit(PRODUCTION, "newTag: 6.7.0", "newTag: 6.8.0")
        self.assertProblem("newer than staging")

    def test_a_release_is_ahead_of_its_own_prerelease(self):
        self.edit(STAGING, "newTag: 6.7.1", "newTag: 6.7.2-rc1")
        self.edit(PRODUCTION, "newTag: 6.7.0", "newTag: 6.7.2")
        self.assertProblem("newer than staging")

    def test_production_image_missing_from_staging_is_rejected(self):
        self.edit(PRODUCTION, "name: ghcr.io/stefanprodan/podinfo", "name: ghcr.io/example/other")
        self.assertProblem("that staging does not")

    def test_missing_overlay_is_reported(self):
        shutil.rmtree(self.path("apps/podinfo/overlays/production"))
        self.assertProblem("overlays/production/kustomization.yaml is missing")


class ReferenceTests(RepoCase):
    def test_missing_resource_file(self):
        self.edit("apps/podinfo/base/kustomization.yaml", "- service.yaml", "- svc.yaml")
        self.assertProblem("svc.yaml does not exist")

    def test_directory_without_kustomization(self):
        (self.path("apps/empty")).mkdir()
        self.edit("apps/staging/kustomization.yaml", "../podinfo/overlays/staging", "../empty")
        self.assertProblem("without kustomization.yaml")

    def test_missing_patch_file(self):
        self.edit(PRODUCTION, "patches:\n", "patches:\n  - path: nope.yaml\n")
        self.assertProblem("patch nope.yaml does not exist")

    def test_remote_resources_are_not_checked(self):
        self.edit("apps/podinfo/base/kustomization.yaml", "- service.yaml",
                  "- service.yaml\n  - https://example.com/extra.yaml")
        self.assertEqual(self.problems(), [])


class FluxTests(RepoCase):
    def test_staging_pointing_at_production_is_rejected(self):
        self.edit("clusters/staging/sync.yaml", "path: ./apps/staging", "path: ./apps/production")
        self.assertProblem("points at another environment")

    def test_missing_path_is_rejected(self):
        self.edit("clusters/production/sync.yaml", "path: ./infrastructure/production",
                  "path: ./infrastructure/prod")
        self.assertProblem("has no kustomization.yaml")

    def test_repository_urls_must_agree(self):
        self.edit("clusters/production/sync.yaml", "soheiltaherpoor/k3s-gitops-starter",
                  "someone-else/k3s-gitops-starter")
        self.assertProblem("URLs differ")

    def test_unknown_dependency_is_rejected(self):
        self.edit("clusters/staging/sync.yaml", "- name: infrastructure", "- name: platformm")
        self.assertProblem("depends on unknown")

    def test_unknown_source_is_rejected(self):
        self.edit("clusters/staging/sync.yaml", "kind: GitRepository\n    name: platform",
                  "kind: GitRepository\n    name: other")
        self.assertProblem("source that is not defined")

    def test_missing_sync_file(self):
        self.path("clusters/staging/sync.yaml").unlink()
        self.assertProblem("clusters/staging/sync.yaml is missing")


class SecretTests(RepoCase):
    def test_plaintext_secret_is_rejected(self):
        for field in ("data", "stringData"):
            with self.subTest(field=field):
                self.write("apps/podinfo/base/secret.yaml", {
                    "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "db"},
                    field: {"password": "aHVudGVyMg=="}})
                self.assertProblem("is in plaintext")

    def test_encrypted_secret_is_accepted(self):
        self.write("apps/podinfo/overlays/staging/db.sops.yaml", ENCRYPTED_SECRET)
        self.assertEqual(self.problems(), [])

    def test_sops_metadata_with_a_plain_value_is_rejected(self):
        doc = dict(ENCRYPTED_SECRET)
        doc["stringData"] = {"password": "ENC[AES256_GCM,data:abc]", "token": "plain"}
        self.write("apps/podinfo/overlays/staging/db.sops.yaml", doc)
        self.assertProblem("values that are not encrypted")

    def test_sops_named_file_without_metadata_is_rejected(self):
        self.write("apps/podinfo/overlays/staging/db.sops.yaml", {
            "apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x"}, "data": {"a": "b"}})
        self.assertProblem("not encrypted")

    def test_secret_without_values_is_fine(self):
        self.write("apps/podinfo/base/sa-token.yaml", {
            "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "t"},
            "type": "kubernetes.io/service-account-token"})
        self.assertEqual(self.problems(), [])

    def test_sops_configuration_file_is_not_mistaken_for_a_secret(self):
        self.assertTrue(self.path(".sops.yaml").exists())
        self.assertEqual([p for p in self.problems() if ".sops.yaml" in p], [])


class NamespaceAndImageTests(RepoCase):
    def test_namespace_needs_a_pod_security_label(self):
        self.write("infrastructure/base/namespace.yaml", {
            "apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "podinfo"}})
        self.assertProblem("pod-security.kubernetes.io/enforce")

    def test_base_image_without_tag_is_rejected(self):
        self.edit("apps/podinfo/base/deployment.yaml", "podinfo:6.7.0", "podinfo")
        self.assertProblem("unpinned image")

    def test_base_image_latest_is_rejected(self):
        self.edit("apps/podinfo/base/deployment.yaml", "podinfo:6.7.0", "podinfo:latest")
        self.assertProblem("unpinned image")

    def test_base_image_digest_is_accepted(self):
        self.edit("apps/podinfo/base/deployment.yaml", "podinfo:6.7.0",
                  "podinfo@sha256:" + "b" * 64)
        self.assertEqual(self.problems(), [])


class PromoteTests(RepoCase):
    def test_dry_run_changes_nothing(self):
        before = self.path(PRODUCTION).read_text(encoding="utf-8")
        changes = gitops.promote(self.root, ["podinfo"], write=False)
        self.assertEqual(len(changes), 1)
        self.assertIn("6.7.0 -> 6.7.1", changes[0])
        self.assertEqual(self.path(PRODUCTION).read_text(encoding="utf-8"), before)

    def test_write_changes_only_the_tag_line(self):
        before = self.path(PRODUCTION).read_text(encoding="utf-8").splitlines()
        gitops.promote(self.root, ["podinfo"], write=True)
        after = self.path(PRODUCTION).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(before), len(after))
        diff = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(diff, [("    newTag: 6.7.0", "    newTag: 6.7.1")])
        self.assertEqual(self.problems(), [])

    def test_promotion_is_idempotent(self):
        gitops.promote(self.root, ["podinfo"], write=True)
        self.assertEqual(gitops.promote(self.root, ["podinfo"], write=True), [])

    def test_promotes_when_staging_moves_on(self):
        self.edit(STAGING, "newTag: 6.7.1", "newTag: 6.8.0")
        gitops.promote(self.root, ["podinfo"], write=True)
        self.assertEqual(gitops.show(self.root, "production", "podinfo")["tag"], "6.8.0")

    def test_missing_production_entry_is_an_error(self):
        self.edit(PRODUCTION, "name: ghcr.io/stefanprodan/podinfo", "name: ghcr.io/example/other")
        with self.assertRaises(gitops.RepoError):
            gitops.promote(self.root, ["podinfo"], write=True)


class ShowAndCliTests(RepoCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gitops.main(["--root", str(self.root)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_show_reports_tag_and_replicas(self):
        info = gitops.show(self.root, "production", "podinfo")
        self.assertEqual(info["tag"], "6.7.0")
        self.assertEqual(info["replicas"], 2)
        self.assertEqual(gitops.show(self.root, "staging", "podinfo")["replicas"], 1)

    def test_cli_show_field_and_json(self):
        code, out, _ = self.run_cli("show", "staging", "--app", "podinfo", "--field", "tag")
        self.assertEqual((code, out.strip()), (0, "6.7.1"))
        code, out, _ = self.run_cli("show", "staging", "--app", "podinfo")
        self.assertEqual(json.loads(out)["image"], "ghcr.io/stefanprodan/podinfo")

    def test_cli_check_exit_codes(self):
        self.assertEqual(self.run_cli("check")[0], 0)
        self.edit(STAGING, "newTag: 6.7.1", "newTag: latest")
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)

    def test_cli_promote_defaults_to_dry_run(self):
        code, out, _ = self.run_cli("promote")
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)
        self.assertEqual(gitops.show(self.root, "production", "podinfo")["tag"], "6.7.0")
        code, out, _ = self.run_cli("promote", "--write")
        self.assertEqual(code, 0)
        self.assertEqual(gitops.show(self.root, "production", "podinfo")["tag"], "6.7.1")

    def test_unreadable_yaml_exits_2(self):
        self.write(STAGING, "images: [unclosed\n")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 2)
        self.assertIn("cannot read as YAML", err)

    def test_show_for_unknown_app_exits_2(self):
        code, _, err = self.run_cli("show", "staging", "--app", "nope")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
