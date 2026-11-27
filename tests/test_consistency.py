"""Checks that the scripts, Makefile and workflow agree with the manifests they drive."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import gitops  # noqa: E402


def load_all(path):
    return [d for d in yaml.safe_load_all(Path(path).read_text(encoding="utf-8")) if d]


class FluxNamesTests(unittest.TestCase):
    """tests/e2e.sh waits on these names, so renaming them must fail here first."""

    def test_sync_files_define_the_names_the_e2e_script_uses(self):
        script = (ROOT / "tests" / "e2e.sh").read_text(encoding="utf-8")
        for env in gitops.ENVS:
            docs = load_all(ROOT / "clusters" / env / "sync.yaml")
            names = {(d["kind"], d["metadata"]["name"]) for d in docs}
            with self.subTest(env=env):
                self.assertIn(("GitRepository", "platform"), names)
                self.assertIn(("Kustomization", "infrastructure"), names)
                self.assertIn(("Kustomization", "apps"), names)
                for needle in ("gitrepository/platform", "kustomization/infrastructure",
                               "kustomization/apps"):
                    self.assertIn(needle, script)

    def test_every_sync_is_in_flux_system(self):
        for env in gitops.ENVS:
            for doc in load_all(ROOT / "clusters" / env / "sync.yaml"):
                self.assertEqual(doc["metadata"]["namespace"], "flux-system")

    def test_apps_wait_for_infrastructure_and_decrypt_with_the_documented_secret(self):
        for env in gitops.ENVS:
            apps = [d for d in load_all(ROOT / "clusters" / env / "sync.yaml")
                    if d["kind"] == "Kustomization" and d["metadata"]["name"] == "apps"][0]
            with self.subTest(env=env):
                self.assertEqual(apps["spec"]["dependsOn"], [{"name": "infrastructure"}])
                self.assertEqual(apps["spec"]["decryption"]["secretRef"]["name"], "sops-age")
                self.assertIs(apps["spec"]["prune"], True)
                self.assertIs(apps["spec"]["wait"], True)


class MakefileTests(unittest.TestCase):
    def test_render_directories_exist_and_are_kustomizations(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        match = re.search(r"^RENDER_DIRS = (.+)$", makefile, re.M)
        self.assertIsNotNone(match)
        dirs = match.group(1).split()
        self.assertGreaterEqual(len(dirs), 6)
        for d in dirs:
            with self.subTest(dir=d):
                self.assertTrue((ROOT / d / "kustomization.yaml").exists())

    def test_every_environment_has_each_kind_of_directory_that_is_rendered(self):
        for env in gitops.ENVS:
            for kind in ("infrastructure", "apps", "clusters"):
                self.assertTrue((ROOT / kind / env / "kustomization.yaml").exists(), (kind, env))

    def test_recipes_use_tabs(self):
        for number, line in enumerate((ROOT / "Makefile").read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("    "):
                self.fail("Makefile line %d is indented with spaces" % number)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))

    def test_e2e_covers_every_environment(self):
        matrix = self.workflow["jobs"]["e2e"]["strategy"]["matrix"]["env"]
        self.assertEqual(sorted(matrix), sorted(gitops.ENVS))

    def test_e2e_never_runs_on_pull_requests(self):
        self.assertIn("pull_request", self.workflow["jobs"]["e2e"]["if"])
        self.assertIn("!=", self.workflow["jobs"]["e2e"]["if"])

    def test_runners_are_pinned(self):
        for name, job in self.workflow["jobs"].items():
            self.assertEqual(job["runs-on"], "ubuntu-24.04", name)


class ScriptTests(unittest.TestCase):
    SCRIPTS = sorted((ROOT / "tests").glob("*.sh"))

    def test_there_are_scripts(self):
        self.assertEqual({p.name for p in self.SCRIPTS}, {"e2e.sh", "sops-roundtrip.sh"})

    def test_scripts_parse_and_fail_fast(self):
        for script in self.SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("set -euo pipefail", script.read_text(encoding="utf-8"))

    def test_e2e_rejects_a_bad_environment_name_before_touching_anything(self):
        result = subprocess.run(["bash", str(ROOT / "tests" / "e2e.sh"), "qa"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


class DocumentationTests(unittest.TestCase):
    def test_sops_config_has_the_rule_the_readme_describes(self):
        config = yaml.safe_load((ROOT / ".sops.yaml").read_text(encoding="utf-8"))
        rule = config["creation_rules"][0]
        self.assertTrue(re.search(rule["path_regex"], "db.sops.yaml"))
        self.assertFalse(re.search(rule["path_regex"], "db.yaml"))
        self.assertIn("stringData", rule["encrypted_regex"])

    def test_no_private_key_material_is_committed(self):
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.name == "test_consistency.py":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn("AGE-SECRET-KEY-", text, path)
            self.assertNotIn("BEGIN PRIVATE KEY", text, path)


if __name__ == "__main__":
    unittest.main()
