"""Tests for install.sh: temp HOME and XDG_CONFIG_HOME, a PATH without hc, no network.

Run: python3 -I -m unittest discover -s tests -p "test_install.py" -v
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_FILES = ("SKILL.md", "README.md", "install.sh")
SKILL_DIRS = ("scripts", "references", "templates")
JUNK = (".git", "tests", ".hc-orchestrate")


class InstallBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hco-install-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.xdg = self.tmp / "xdg"
        self.tdir = self.xdg / "herdr-core" / "templates"
        self.checkout = self.make_checkout(self.tmp / "checkout")

    @staticmethod
    def make_checkout(path):
        """A checkout with what the skill needs plus what a working copy accumulates."""
        path.mkdir(parents=True)
        for name in SKILL_FILES:
            shutil.copy(ROOT / name, path / name)
        for name in SKILL_DIRS:
            shutil.copytree(ROOT / name, path / name, ignore=shutil.ignore_patterns("__pycache__"))
        (path / "scripts" / "__pycache__").mkdir()
        (path / "scripts" / "__pycache__" / "x.pyc").write_bytes(b"x")
        (path / ".git").mkdir()
        (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (path / "tests").mkdir()
        (path / "tests" / "test_x.py").write_text("x = 1\n")
        (path / ".hc-orchestrate" / "run1" / "out").mkdir(parents=True)
        (path / ".hc-orchestrate" / "run1" / "out" / "RESULT.md").write_text("state\n")
        return path

    def env(self, home=None):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home or self.home), "XDG_CONFIG_HOME": str(self.xdg)}
        return env

    def install(self, *args, script=None, home=None, cwd=None):
        script = script or self.checkout / "install.sh"
        return subprocess.run(["sh", str(script), *args], env=self.env(home), cwd=str(cwd or self.tmp),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def skill(self, home=None, sub=".claude/skills"):
        return (home or self.home) / sub / "hc-orchestrate"


class InstallCopy(InstallBase):
    def test_path_has_no_hc(self):
        self.assertIsNone(shutil.which("hc", path=self.env()["PATH"]), "the test needs a PATH without hc")

    def test_copy_installs_only_the_skill(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        for sub in (".claude/skills", ".agents/skills"):
            dest = self.skill(sub=sub)
            self.assertFalse(dest.is_symlink())
            for name in SKILL_FILES:
                self.assertTrue((dest / name).is_file(), f"{sub}: {name} missing")
            for name in SKILL_DIRS:
                self.assertTrue((dest / name).is_dir(), f"{sub}: {name} missing")
            for name in JUNK:
                self.assertFalse((dest / name).exists(), f"{sub}: {name} was copied")
            self.assertFalse((dest / "scripts" / "__pycache__").exists())
            leftovers = [p.name for p in dest.parent.iterdir() if p.name != "hc-orchestrate"]
            self.assertEqual(leftovers, [], "temp folder left next to the install")
        self.assertTrue((self.tdir / "hco-claude.md").is_file())
        self.assertIn("hc is not installed", r.stdout)

    def test_rerun_is_safe(self):
        self.assertEqual(self.install().returncode, 0)
        marker = self.skill() / "stale.txt"
        marker.write_text("old install\n")
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(marker.exists(), "the old install was not replaced")
        self.assertTrue((self.skill() / "SKILL.md").is_file())
        self.assertTrue((self.checkout / "SKILL.md").is_file())
        self.assertNotIn("kept your", r.stdout)

    def test_rerun_from_the_installed_copy_is_safe(self):
        self.assertEqual(self.install().returncode, 0)
        r = self.install(script=self.skill() / "install.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.skill() / "SKILL.md").is_file())
        self.assertIn("already lives at", r.stdout)


class InstallLink(InstallBase):
    def test_link_links(self):
        r = self.install("--link")
        self.assertEqual(r.returncode, 0, r.stderr)
        for sub in (".claude/skills", ".agents/skills"):
            dest = self.skill(sub=sub)
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(), self.checkout)
        self.assertTrue((self.checkout / ".git").is_dir())


class InstallSymlinkedHome(InstallBase):
    def test_checkout_inside_a_symlinked_home_survives(self):
        real = self.tmp / "real-home"
        real.mkdir()
        link = self.tmp / "link-home"
        link.symlink_to(real)
        checkout = self.make_checkout(real / ".claude" / "skills" / "hc-orchestrate")
        # run through the physical path; $HOME names the same folder through the symlink
        r = self.install(script=checkout / "install.sh", home=link)
        self.assertTrue((checkout / "SKILL.md").is_file(), f"the checkout was deleted\n{r.stdout}\n{r.stderr}")
        self.assertTrue((checkout / ".git").is_dir())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already lives at", r.stdout)
        # and from inside it, through the other spelling of the path
        r = self.install(script="install.sh", home=link, cwd=link / ".claude" / "skills" / "hc-orchestrate")
        self.assertTrue((checkout / "SKILL.md").is_file(), f"the checkout was deleted\n{r.stdout}\n{r.stderr}")
        self.assertEqual(r.returncode, 0, r.stderr)


class InstallTemplates(InstallBase):
    def setUp(self):
        super().setUp()
        self.tdir.mkdir(parents=True)
        self.mine = self.tdir / "hco-claude.md"
        self.mine.write_text("command: my-own-claude\n")

    def test_differing_template_is_kept_without_force_and_said_so(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mine.read_text(), "command: my-own-claude\n")
        self.assertIn("kept your hco-claude.md", r.stdout)
        tail = r.stdout.split("kept your hco-claude.md", 1)[1]
        self.assertIn("--force", tail, "the end of the output must say how to replace the kept templates")
        self.assertIn("hco-claude.md", tail)
        self.assertTrue((self.tdir / "hco-codex.md").is_file(), "the other template is still installed")

    def test_force_replaces_the_template(self):
        r = self.install("--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mine.read_text(), (ROOT / "templates" / "hco-claude.md").read_text())
        self.assertNotIn("kept your", r.stdout)

    def test_no_notice_when_nothing_was_kept(self):
        self.assertEqual(self.install("--force").returncode, 0)
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("kept", r.stdout)


if __name__ == "__main__":
    unittest.main()
