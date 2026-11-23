# SPDX-License-Identifier: BSD-3-Clause

"""
project_auto_release.py, run against git repositories made for each test, with
'origin' a bare repository on disk and gh replaced by a script that logs its
arguments and answers as the test sets it to
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("par", ROOT / "project_auto_release.py")
assert spec and spec.loader
par = importlib.util.module_from_spec(spec)
spec.loader.exec_module(par)

PATTERN = r'^__version__ = "([^"]+)"$'

CHANGELOG = """# Changelog

## 1.2.3

What 1.2.3 brings.

* and a list

## 1.2.2

What came before.
"""

# Answers 'gh api .../pulls' with $FAKE_PR, 'gh run list' with $FAKE_RUN, or with
# $FAKE_OTHER_RUN when it is set and the list is not of pull_request runs, 'gh run
# view' with $FAKE_JOBS, and 'gh release view' with success when $FAKE_RELEASED is
# 'true'. The first $FAKE_FAILS calls of '$FAKE_FAIL_ON' fail with $FAKE_ERROR.
FAKE_GH = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG"
if [[ "$1 $2" == "$FAKE_FAIL_ON" ]]; then
  failed=$(grep -c "^$FAKE_FAIL_ON" "$FAKE_LOG")
  if (( failed <= FAKE_FAILS )); then
    echo "$FAKE_ERROR" >&2
    exit 1
  fi
fi
case "$1 $2" in
  "api "*) echo "$FAKE_PR" ;;
  "run list")
    if [[ -n "$FAKE_OTHER_RUN" && " $* " != *" --event pull_request "* ]]; then
      echo "$FAKE_OTHER_RUN"
    else
      echo "$FAKE_RUN"
    fi ;;
  "run view") printf '%b' "$FAKE_JOBS" ;;
  "release view") [ "$FAKE_RELEASED" = "true" ] ;;
esac
"""


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Project(unittest.TestCase):
    """A clone, 'work', of a bare repository, 'origin', whose main has one commit"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.work = self.tmp / "work"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.work))
        self.commit("feat: the first", version="1.2.2", changelog=CHANGELOG)
        git(self.work, "push", "-q", "origin", "HEAD:main")

        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH)
        (bin_dir / "gh").chmod(0o755)
        self.log = self.tmp / "gh.log"
        self.log.touch()
        self.out = self.tmp / "output"
        self.env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_PR": "",
            "FAKE_RUN": "",
            "FAKE_JOBS": "",
            "FAKE_RELEASED": "false",
            "FAKE_FAIL_ON": "",
            "FAKE_FAILS": "0",
            "FAKE_ERROR": "HTTP 502: Server Error (https://api.github.com/...)",
            "GITHUB_OUTPUT": str(self.out),
            "RUNNER_TEMP": str(self.tmp),
            "PAR_VERSION_FILE": "src/foo/__init__.py",
            "PAR_VERSION_PATTERN": PATTERN,
            "PAR_CHANGELOG": "CHANGELOG.md",
            "PAR_REPOSITORY": "owner/foo",
        }
        cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, cwd)

    def commit(self, subject, version=None, changelog=None):
        if version is not None:
            path = self.work / "src/foo/__init__.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f'__version__ = "{version}"\n')
        if changelog is not None:
            (self.work / "CHANGELOG.md").write_text(changelog)
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "--allow-empty", "-m", subject)
        return git(self.work, "rev-parse", "HEAD")

    def call(self, command, **env):
        """Run command with the test's environment; its exit code, stdout, and outputs"""

        environ = {**os.environ, **self.env, **env}
        self.out.write_text("")
        stdout = io.StringIO()
        old = os.environ.copy()
        os.environ.clear()
        os.environ.update(environ)
        try:
            with contextlib.redirect_stdout(stdout):
                code = par.main(["project_auto_release.py", command])
        finally:
            os.environ.clear()
            os.environ.update(old)
        outputs = dict(line.split("=", 1) for line in self.out.read_text().splitlines())
        return code, stdout.getvalue(), outputs

    def gh_calls(self):
        return self.log.read_text().splitlines()


@mock.patch.object(par, "RETRY_DELAYS", (0, 0, 0))
class Retry(Project):
    def setUp(self):
        super().setUp()
        self.sha = self.commit("ver: bump to v1.2.3", version="1.2.3")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.env.update(PAR_SHA=self.sha, PAR_VERSION="1.2.3")

    def calls_of(self, command):
        return [c for c in self.gh_calls() if c.startswith(command)]

    def test_a_read_is_repeated_while_github_answers_5xx(self):
        code, stdout, _ = self.call(
            "release", FAKE_FAIL_ON="release view", FAKE_FAILS="2", FAKE_RELEASED="true"
        )
        self.assertEqual(code, 0, stdout)
        self.assertEqual(len(self.calls_of("release view")), 3)
        self.assertEqual(stdout.count("::warning::"), 2)
        self.assertFalse(self.calls_of("release create"))

    def test_a_read_fails_once_the_repeats_are_spent(self):
        code, stdout, _ = self.call(
            "release",
            FAKE_FAIL_ON="release upload",
            FAKE_FAILS="9",
            PAR_ASSETS=__file__,
        )
        self.assertEqual(code, 1, stdout)
        self.assertEqual(len(self.calls_of("release upload")), 4)
        self.assertIn("::error::gh release upload", stdout)

    def test_an_error_other_than_5xx_is_not_repeated(self):
        code, stdout, _ = self.call(
            "release",
            FAKE_FAIL_ON="release upload",
            FAKE_FAILS="9",
            FAKE_ERROR="HTTP 404: Not Found",
            PAR_ASSETS=__file__,
        )
        self.assertEqual(code, 1, stdout)
        self.assertEqual(len(self.calls_of("release upload")), 1)

    def test_a_create_is_not_repeated(self):
        code, stdout, _ = self.call(
            "release", FAKE_FAIL_ON="release create", FAKE_FAILS="9"
        )
        self.assertEqual(code, 1, stdout)
        self.assertEqual(len(self.calls_of("release create")), 1)

    def test_a_dispatch_is_not_repeated(self):
        code, stdout, _ = self.call(
            "release",
            FAKE_FAIL_ON="workflow run",
            FAKE_FAILS="9",
            PAR_DISPATCH="verify.yml",
        )
        self.assertEqual(code, 1, stdout)
        self.assertEqual(len(self.calls_of("workflow run")), 1)


class Parsing(unittest.TestCase):
    def test_bumped(self):
        self.assertEqual(par.bumped("ver: bump to v1.2.3"), "1.2.3")
        self.assertEqual(par.bumped("ver: bump to v25.11"), "25.11")
        self.assertIsNone(par.bumped("ver: bump to 1.2.3"))
        self.assertIsNone(par.bumped("fix: ver: bump to v1.2.3"))
        self.assertIsNone(par.bumped("ver: bump to v1.2.3 and more"))

    def test_version_in(self):
        self.assertEqual(
            par.version_in('x\n__version__ = "1.2.3"\n', PATTERN, "f"), "1.2.3"
        )
        self.assertEqual(
            par.version_in("VERSION ?= 0.12.0\n", r"^VERSION \?= (\S+)$", "f"), "0.12.0"
        )
        with self.assertRaisesRegex(par.Refusal, "no line matching"):
            par.version_in("nothing", PATTERN, "f")
        with self.assertRaisesRegex(par.Refusal, "no group"):
            par.version_in("1.2.3", r"^\S+$", "f")

    def test_notes_for(self):
        self.assertEqual(
            par.notes_for(CHANGELOG, "1.2.3"), "What 1.2.3 brings.\n\n* and a list"
        )
        self.assertEqual(par.notes_for(CHANGELOG, "1.2.2"), "What came before.")
        self.assertEqual(par.notes_for(CHANGELOG, "1.2"), "")


class Bump(Project):
    def test_complete(self):
        sha = self.commit("ver: bump to v1.2.3", version="1.2.3")
        version, notes = par.bump(sha, "src/foo/__init__.py", PATTERN, "CHANGELOG.md")
        self.assertEqual(version, "1.2.3")
        self.assertTrue(notes.startswith("What 1.2.3 brings."))

    def test_without_changelog(self):
        sha = self.commit("ver: bump to v1.2.9", version="1.2.9")
        self.assertEqual(
            par.bump(sha, "src/foo/__init__.py", PATTERN, ""), ("1.2.9", "")
        )

    def test_not_a_bump(self):
        sha = self.commit("feat: more", version="1.2.3")
        with self.assertRaisesRegex(par.Refusal, "not a version bump"):
            par.bump(sha, "src/foo/__init__.py", PATTERN, "CHANGELOG.md")

    def test_version_file_disagrees(self):
        sha = self.commit("ver: bump to v1.2.3")
        with self.assertRaisesRegex(
            par.Refusal, "bumps to 1.2.3, but src/foo/__init__.py has 1.2.2"
        ):
            par.bump(sha, "src/foo/__init__.py", PATTERN, "CHANGELOG.md")

    def test_no_changelog_section(self):
        sha = self.commit("ver: bump to v1.2.4", version="1.2.4")
        with self.assertRaisesRegex(par.Refusal, "no '## 1.2.4' section"):
            par.bump(sha, "src/foo/__init__.py", PATTERN, "CHANGELOG.md")

    def test_missing_file(self):
        sha = self.commit("ver: bump to v1.2.3", version="1.2.3")
        with self.assertRaisesRegex(par.Refusal, "VERSION is not in"):
            par.bump(sha, "VERSION", r"^(\S+)$", "")


class Tagged(Project):
    def test_lightweight_and_annotated(self):
        sha = git(self.work, "rev-parse", "HEAD")
        self.assertEqual(par.tagged("v1.2.2"), "")
        git(self.work, "tag", "v1.2.2")
        git(self.work, "tag", "-a", "-m", "annotated", "v1.2.2-a")
        git(self.work, "push", "-q", "origin", "v1.2.2", "v1.2.2-a")
        self.assertEqual(par.tagged("v1.2.2"), sha)
        self.assertEqual(par.tagged("v1.2.2-a"), sha)
        par.untagged_elsewhere("v1.2.2", sha)
        other = self.commit("feat: other")
        with self.assertRaisesRegex(par.Refusal, "v1.2.2 is a tag of another commit"):
            par.untagged_elsewhere("v1.2.2", other)


class Ready(Project):
    def pull_request(self, *commits):
        git(self.work, "checkout", "-q", "-b", "pr")
        return [self.commit(*args) for args in commits][-1]

    def test_not_a_release(self):
        sha = self.pull_request(("feat: more",))
        code, stdout, outputs = self.call("ready", PAR_SHA=sha, PAR_BASE="main")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(outputs, {"release": "false", "version": ""})

    def test_release(self):
        sha = self.pull_request(("feat: more",), ("ver: bump to v1.2.3", "1.2.3"))
        code, stdout, outputs = self.call("ready", PAR_SHA=sha, PAR_BASE="main")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(outputs, {"release": "true", "version": "1.2.3"})

    def test_behind_base(self):
        sha = self.pull_request(("ver: bump to v1.2.3", "1.2.3"))
        git(self.work, "checkout", "-q", "main")
        self.commit("fix: on main meanwhile")
        git(self.work, "push", "-q", "origin", "main")
        code, stdout, _ = self.call("ready", PAR_SHA=sha, PAR_BASE="main")
        self.assertEqual(code, 1)
        self.assertIn("rebase on main", stdout)

    def test_incomplete_bump(self):
        sha = self.pull_request(("ver: bump to v1.2.3",))
        code, stdout, _ = self.call("ready", PAR_SHA=sha, PAR_BASE="main")
        self.assertEqual(code, 1)
        self.assertIn("but src/foo/__init__.py has 1.2.2", stdout)


class Check(Project):
    def merged(self, head_tree_differs=False):
        """A bump on main, merged from pull request 7, whose head is that commit
        or, when head_tree_differs, a commit with another tree"""

        sha = self.commit("ver: bump to v1.2.3", version="1.2.3")
        head = sha
        if head_tree_differs:
            head = self.commit(
                "feat: only on the pull request", changelog=CHANGELOG + "\nmore\n"
            )
            git(self.work, "reset", "-q", "--hard", sha)
        git(self.work, "push", "-q", "origin", "HEAD:main", f"{head}:refs/pull/7/head")
        self.env.update(
            PAR_SHA=sha, FAKE_PR="7", FAKE_RUN="42", PAR_WORKFLOWS="verify.yml test.yml"
        )
        return sha, head

    def test_releases(self):
        _, head = self.merged()
        code, stdout, outputs = self.call("check")
        self.assertEqual(code, 0, stdout)
        notes = self.tmp / "notes-v1.2.3.md"
        self.assertEqual(
            outputs,
            {
                "version": "1.2.3",
                "released": "false",
                "pr": "7",
                "pr-head": head,
                "runs": '{"verify.yml": 42, "test.yml": 42}',
                "notes-file": str(notes),
            },
        )
        self.assertTrue(notes.read_text().startswith("What 1.2.3 brings."))
        self.assertIn(
            f"run list --repo owner/foo --workflow test.yml --commit {head}",
            "\n".join(self.gh_calls()),
        )

    def test_a_run_not_of_the_pull_request_is_not_taken(self):
        self.merged()
        code, stdout, outputs = self.call("check", FAKE_OTHER_RUN="99")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(outputs["runs"], '{"verify.yml": 42, "test.yml": 42}')

    def test_released_already(self):
        self.merged()
        code, stdout, outputs = self.call("check", FAKE_RELEASED="true")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(outputs["released"], "true")

    def test_generated_notes(self):
        self.merged()
        code, stdout, outputs = self.call("check", PAR_CHANGELOG="")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(outputs["notes-file"], "")

    def test_no_pull_request(self):
        self.merged()
        code, stdout, _ = self.call("check", FAKE_PR="")
        self.assertEqual(code, 1)
        self.assertIn("no pull request merged", stdout)

    def test_pull_request_behind(self):
        self.merged(head_tree_differs=True)
        code, stdout, _ = self.call("check")
        self.assertEqual(code, 1)
        self.assertIn("#7 was behind main when merged", stdout)

    def test_workflow_not_passed(self):
        self.merged()
        code, stdout, _ = self.call("check", FAKE_RUN="")
        self.assertEqual(code, 1)
        self.assertIn("no successful verify.yml run of #7's head", stdout)

    def test_jobs_passed(self):
        self.merged()
        code, stdout, _ = self.call(
            "check",
            PAR_JOBS="verify.yml: ci-result\nverify.yml: hardware\n",
            FAKE_JOBS="ci-result\\tsuccess\\nhardware (nvme)\\tsuccess\\n"
            "hardware (zns)\\tsuccess\\n",
        )
        self.assertEqual(code, 0, stdout)
        self.assertIn(
            "run view 42 --repo owner/foo --json jobs", "\n".join(self.gh_calls())
        )

    def test_job_skipped(self):
        self.merged()
        code, stdout, _ = self.call(
            "check",
            PAR_JOBS="verify.yml: hardware",
            FAKE_JOBS="hardware (nvme)\\tsuccess\\nhardware (zns)\\tskipped\\n",
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "job 'hardware (zns)' of verify.yml run 42 concluded skipped", stdout
        )

    def test_reusable_workflow(self):
        self.merged()
        jobs = (
            "emulated / (debian, pcie)\\tsuccess\\n"
            "docgen / docgen (debian)\\tsuccess\\n"
            "hardware\\tskipped\\n"
        )
        code, stdout, _ = self.call(
            "check", PAR_JOBS="verify.yml: emulated\nverify.yml: docgen", FAKE_JOBS=jobs
        )
        self.assertEqual(code, 0, stdout)
        code, stdout, _ = self.call(
            "check", PAR_JOBS="verify.yml: hardware", FAKE_JOBS=jobs
        )
        self.assertEqual(code, 1)
        self.assertIn("job 'hardware' of verify.yml run 42 concluded skipped", stdout)

    def test_job_missing(self):
        self.merged()
        code, stdout, _ = self.call(
            "check", PAR_JOBS="test.yml: gpu", FAKE_JOBS="cpu\\tsuccess\\n"
        )
        self.assertEqual(code, 1)
        self.assertIn("test.yml run 42 has no job 'gpu'", stdout)

    def test_jobs_of_another_workflow(self):
        self.merged()
        code, stdout, _ = self.call("check", PAR_JOBS="other.yml: gpu")
        self.assertEqual(code, 1)
        self.assertIn("jobs names other.yml, which workflows does not", stdout)

    def test_tag_of_another_commit(self):
        self.merged()
        git(self.work, "push", "-q", "origin", "HEAD~1:refs/tags/v1.2.3")
        code, stdout, _ = self.call("check")
        self.assertEqual(code, 1)
        self.assertIn("v1.2.3 is a tag of another commit", stdout)


class Release(Project):
    def setUp(self):
        super().setUp()
        self.sha = self.commit("ver: bump to v1.2.3", version="1.2.3")
        git(self.work, "push", "-q", "origin", "HEAD:main")
        self.env.update(PAR_SHA=self.sha, PAR_VERSION="1.2.3")

    def test_with_notes(self):
        code, stdout, outputs = self.call("release", PAR_NOTES_FILE="/n.md")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(outputs, {"tag": "v1.2.3"})
        self.assertIn(
            f"release create v1.2.3 --repo owner/foo --target {self.sha} --title v1.2.3 --notes-file /n.md",
            self.gh_calls(),
        )

    def test_generated_notes(self):
        code, stdout, _ = self.call("release")
        self.assertEqual(code, 0, stdout)
        self.assertTrue(self.gh_calls()[-1].endswith("--title v1.2.3 --generate-notes"))

    def test_released_already(self):
        code, stdout, _ = self.call("release", FAKE_RELEASED="true")
        self.assertEqual(code, 0, stdout)
        self.assertFalse([c for c in self.gh_calls() if c.startswith("release create")])

    def test_assets(self):
        (self.tmp / "dist").mkdir()
        for name in ("foo-1.2.3.tar.gz", "foo-1.2.3-py3-none-any.whl"):
            (self.tmp / "dist" / name).touch()
        code, stdout, _ = self.call("release", PAR_ASSETS=f"{self.tmp}/dist/*\n")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(
            self.gh_calls()[-1],
            f"release upload v1.2.3 --repo owner/foo --clobber"
            f" {self.tmp}/dist/foo-1.2.3-py3-none-any.whl {self.tmp}/dist/foo-1.2.3.tar.gz",
        )

    def test_asset_missing(self):
        code, stdout, _ = self.call("release", PAR_ASSETS=f"{self.tmp}/nothing/*")
        self.assertEqual(code, 1)
        self.assertIn("no file matches the asset", stdout)

    def test_major_tag(self):
        git(self.work, "push", "-q", "origin", "HEAD~1:refs/tags/v1")
        code, stdout, _ = self.call("release", PAR_MAJOR_TAG="true")
        self.assertEqual(code, 0, stdout)
        self.assertEqual(git(self.origin, "rev-parse", "v1"), self.sha)

    def test_title(self):
        code, stdout, _ = self.call("release", PAR_TITLE="Release Notes: {tag}")
        self.assertEqual(code, 0, stdout)
        self.assertIn(
            "--title Release Notes: v1.2.3 --generate-notes", self.gh_calls()[-1]
        )

    def test_title_unknown_field(self):
        code, stdout, _ = self.call("release", PAR_TITLE="{name} {tag}")
        self.assertEqual(code, 1)
        self.assertIn("only {version} and {tag}", stdout)
        self.assertEqual(self.gh_calls(), [])

    def test_dispatch(self):
        code, stdout, _ = self.call(
            "release", PAR_DISPATCH="verify.yml job=docgen\n\nextended.yml ref={tag}\n"
        )
        self.assertEqual(code, 0, stdout)
        self.assertEqual(
            self.gh_calls()[-2:],
            ["workflow run verify.yml --repo owner/foo --ref v1.2.3 -f job=docgen",
             "workflow run extended.yml --repo owner/foo --ref v1.2.3 -f ref=v1.2.3"],
        )  # fmt: skip

    def test_dispatch_input_malformed(self):
        code, stdout, _ = self.call("release", PAR_DISPATCH="verify.yml docgen")
        self.assertEqual(code, 1)
        self.assertIn("an input is not 'name=value'", stdout)
        self.assertEqual(self.gh_calls(), [])

    def test_tag_of_another_commit(self):
        git(self.work, "push", "-q", "origin", "HEAD~1:refs/tags/v1.2.3")
        code, stdout, _ = self.call("release")
        self.assertEqual(code, 1)
        self.assertIn("v1.2.3 is a tag of another commit", stdout)
        self.assertEqual(self.gh_calls(), [])


if __name__ == "__main__":
    unittest.main()
