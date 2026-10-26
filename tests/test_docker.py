import json
import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DockerDemoImageTest(unittest.TestCase):
    """The hosted image serves the gated live server and open replay."""

    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text()
        self.ignored = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
                        if line.strip() and not line.startswith("#")]

    def command(self):
        cmd = json.loads(re.search(r"^CMD (.+)$", self.dockerfile, re.M)[1])
        self.assertEqual(cmd[:2], ["sh", "-c"])
        return shlex.split(cmd[2])

    def test_runs_demo_on_all_interfaces_and_port_from_env(self):
        args = self.command()
        self.assertEqual(args[:4], ["exec", "python3", "-m", "papertrail.web"])
        self.assertNotIn("--demo", args)
        self.assertEqual(args[args.index("--host") + 1], "0.0.0.0")
        self.assertEqual(args[args.index("--port") + 1], "$PORT")
        self.assertRegex(self.dockerfile, r"(?m)^ENV .*\bPORT=8000\b")
        self.assertNotIn("--db", args)

    def test_image_has_demo_assets_and_no_secrets(self):
        copied = re.findall(r"(?m)^COPY (\S+)", self.dockerfile)
        samples = json.loads((ROOT / "papertrail/web/demo/samples.json").read_text())
        for path in ["papertrail"] + [item["photo"] for item in samples]:
            self.assertIn(path.split("/")[0], copied)
            self.assertNotIn(path.split("/")[0], self.ignored)
        self.assertNotIn("COPY .", self.dockerfile)
        for pattern in (".git", ".env", ".env.*", "*.key"):
            self.assertIn(pattern, self.ignored)

    def test_runs_as_non_root(self):
        users = re.findall(r"(?m)^USER (\S+)", self.dockerfile)
        self.assertTrue(users)
        self.assertNotIn(users[-1], {"root", "0"})


if __name__ == "__main__":
    unittest.main()


class HuggingFaceSpaceBundleTest(unittest.TestCase):
    def test_bundle_is_docker_space_with_demo_only(self):
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "space"
            subprocess.run(["sh", str(ROOT / "scripts/build_hf_space.sh"), str(out)], check=True,
                           capture_output=True)
            readme = (out / "README.md").read_text()
            meta = readme.split("---\n")[1]
            self.assertIn("sdk: docker\n", meta)
            self.assertIn("app_port: 8000\n", meta)
            self.assertLessEqual(len(re.search(r"short_description: (.*)", meta)[1]), 60)
            self.assertTrue(readme.endswith((ROOT / "README.md").read_text()))
            self.assertEqual((ROOT / "Dockerfile").read_text(), (out / "Dockerfile").read_text())
            self.assertEqual({".dockerignore", "Dockerfile", "LICENSE", "README.md", "papertrail", "samples"},
                             {p.name for p in out.iterdir()})
            for sample in json.loads((ROOT / "papertrail/web/demo/samples.json").read_text()):
                self.assertTrue((out / sample["photo"]).is_file())
                self.assertTrue((out / f"papertrail/web/demo/{sample['id']}.verify.json").is_file())
            self.assertFalse(list(out.rglob("__pycache__")))
            again = subprocess.run(["sh", str(ROOT / "scripts/build_hf_space.sh"), str(out)], capture_output=True)
            self.assertNotEqual(0, again.returncode)
