#!/usr/bin/env python3
# SPDX-License-Identifier: BSD-3-Clause

"""
Release a project when a commit with the subject 'ver: bump to v<version>' lands
on its main branch

  ready    on a pull request: whether its last commit is a version bump, and if
           so, that the bump is complete and the branch is not behind its base
  check    on main: that the commit is a complete version bump, and that the
           pull request that merged it was tested on the same tree
  release  tag the commit, create its GitHub Release, attach assets, move the
           major tag, and dispatch workflows on the tag

Each command reads its inputs from PAR_* environment variables, which the
action.yml of the same name sets, and writes its outputs to $GITHUB_OUTPUT. It
runs git in the current directory, a clone of the repository being released,
with 'origin' pointing at it.
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SUBJECT = re.compile(r"^ver: bump to v(\S+)$")


class Refusal(Exception):
    """A reason the release does not go ahead"""


# Seconds to wait before each repeat of a command GitHub answered with a 5xx
RETRY_DELAYS = (5, 15, 45)


def attempt(cmd: tuple[str, ...], retry: bool) -> subprocess.CompletedProcess:
    """Run cmd; with retry, repeat it after each of RETRY_DELAYS while gh fails
    with an HTTP 5xx, which GitHub returns for a request it may succeed on later.
    Only a command that is safe to repeat may retry"""

    for delay in (*RETRY_DELAYS, None) if retry else (None,):
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0 or delay is None:
            break
        if not re.search(r"\bHTTP 5\d\d\b", result.stderr):
            break
        print(f"::warning::{' '.join(cmd)}: {result.stderr.strip()}; again in {delay}s")
        time.sleep(delay)
    return result


def run(*cmd: str, retry: bool = False) -> str:
    result = attempt(cmd, retry)
    if result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, result.stdout, result.stderr
        )
    return result.stdout


def succeeds(*cmd: str, retry: bool = False) -> bool:
    return attempt(cmd, retry).returncode == 0


def output(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(
        f"{key.replace('_', '-')}={value}\n" for key, value in values.items()
    )
    if path:
        with open(path, "a") as file:
            file.write(lines)
    else:
        print(lines, end="")


def bumped(subject: str) -> str | None:
    """The version that a 'ver: bump to v<version>' subject names, or None"""

    match = SUBJECT.match(subject.strip())
    return match.group(1) if match else None


def version_in(text: str, pattern: str, path: str) -> str:
    """The version that the first group of pattern captures in text, read from path"""

    if re.compile(pattern).groups < 1:
        raise Refusal(
            f"version-pattern {pattern!r} has no group to capture the version"
        )
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise Refusal(f"{path} has no line matching {pattern!r}")
    return match.group(1)


def notes_for(changelog: str, version: str) -> str:
    """The text of the '## <version>' section of changelog, or '' when it has none"""

    section = re.search(
        rf"^## {re.escape(version)}\n(.*?)(?=^## |\Z)",
        changelog,
        re.MULTILINE | re.DOTALL,
    )
    return section.group(1).strip() if section else ""


def have(sha: str) -> None:
    """Fetch sha from origin unless the clone has it"""

    if not succeeds("git", "cat-file", "-e", f"{sha}^{{commit}}"):
        run("git", "fetch", "--quiet", "--depth", "1", "origin", sha)


def show(sha: str, path: str) -> str:
    try:
        return run("git", "show", f"{sha}:{path}")
    except subprocess.CalledProcessError:
        raise Refusal(f"{path} is not in {sha[:12]}") from None


def bump(sha: str, version_file: str, version_pattern: str, changelog: str):
    """The version that commit sha bumps to, and its notes from changelog

    Raises Refusal when the subject of sha is not a version bump, when
    version_file at sha does not carry the version, or when changelog is given
    and has no section for it.
    """

    subject = run("git", "log", "-1", "--format=%s", sha).strip()
    version = bumped(subject)
    if version is None:
        raise Refusal(f"not a version bump: {subject!r}")

    found = version_in(show(sha, version_file), version_pattern, version_file)
    if found != version:
        raise Refusal(f"the commit bumps to {version}, but {version_file} has {found}")

    notes = ""
    if changelog:
        notes = notes_for(show(sha, changelog), version)
        if not notes:
            raise Refusal(f"{changelog} has no '## {version}' section")

    return version, notes


def tagged(tag: str) -> str:
    """The commit that tag names on origin, or '' when origin has no such tag"""

    refs = {}
    for line in run("git", "ls-remote", "origin", f"refs/tags/{tag}*").splitlines():
        sha, ref = line.split("\t")
        refs[ref] = sha
    return refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}", ""))


def untagged_elsewhere(tag: str, sha: str) -> None:
    found = tagged(tag)
    if found and found != sha:
        raise Refusal(f"{tag} is a tag of another commit, {found[:12]}")


def required_jobs(lines: str, workflows: list[str]) -> list[tuple[str, str]]:
    """The (workflow, job name) pairs of lines such as 'verify.yml: ci-result'"""

    jobs = []
    for line in lines.splitlines():
        if not line.strip():
            continue
        workflow, sep, name = line.partition(":")
        workflow, name = workflow.strip(), name.strip()
        if not sep or not name:
            raise Refusal(f"jobs line {line!r} is not '<workflow>: <job name>'")
        if workflow not in workflows:
            raise Refusal(f"jobs names {workflow}, which workflows does not")
        jobs.append((workflow, name))
    return jobs


def passed(repo: str, workflow: str, run_id: int, names: list[str]) -> None:
    """Raise Refusal unless each job named in names concluded success in the run

    A name matches the job of that name, each 'name (...)' of a matrix, and each
    'name / ...' that a call of a reusable workflow named name runs. A job
    skipped, as one behind a condition that did not hold, has not passed; a
    skipped call is listed by its bare name.
    """

    conclusions = {}
    for line in run(
        "gh", "run", "view", str(run_id), "--repo", repo, "--json", "jobs",
        "--jq", ".jobs[] | [.name, .conclusion] | @tsv", retry=True,
    ).splitlines():  # fmt: skip
        job, _, conclusion = line.partition("\t")
        conclusions[job] = conclusion
    for name in names:
        found = {
            job: conclusion
            for job, conclusion in conclusions.items()
            if job == name or job.startswith((f"{name} (", f"{name} / "))
        }
        if not found:
            raise Refusal(f"{workflow} run {run_id} has no job {name!r}")
        for job, conclusion in found.items():
            if conclusion != "success":
                raise Refusal(
                    f"job {job!r} of {workflow} run {run_id} concluded"
                    f" {conclusion or 'nothing'}, not success"
                )


def tested(repo: str, sha: str, workflows: list[str], jobs: list[tuple[str, str]]):
    """The pull request that merged sha, its head, and a successful pull_request
    run of each workflow on that head in which each of jobs passed; raises Refusal
    when the head's tree is not sha's"""

    pr = run(
        "gh", "api", f"repos/{repo}/commits/{sha}/pulls", "--jq", ".[0].number // empty",
        retry=True,
    ).strip()  # fmt: skip
    if not pr:
        raise Refusal(f"no pull request merged {sha[:12]}; release by merging one")

    run("git", "fetch", "--quiet", "--depth", "1", "origin", f"refs/pull/{pr}/head")
    head = run("git", "rev-parse", "FETCH_HEAD").strip()
    tree = run("git", "rev-parse", f"{head}^{{tree}}")
    if tree != run("git", "rev-parse", f"{sha}^{{tree}}"):
        raise Refusal(
            f"#{pr} was behind main when merged, so its checks ran on another tree"
        )

    runs = {}
    for workflow in workflows:
        found = run(
            "gh", "run", "list", "--repo", repo, "--workflow", workflow,
            "--commit", head, "--event", "pull_request",
            "--status", "success", "--limit", "1",
            "--json", "databaseId", "--jq", ".[0].databaseId // empty", retry=True,
        ).strip()  # fmt: skip
        if not found:
            raise Refusal(f"no successful {workflow} run of #{pr}'s head, {head[:12]}")
        runs[workflow] = int(found)
        passed(repo, workflow, runs[workflow], [n for w, n in jobs if w == workflow])

    return pr, head, runs


def ready(env) -> None:
    sha, base = env["PAR_SHA"], env["PAR_BASE"]
    if run("git", "rev-parse", "--is-shallow-repository").strip() == "true":
        run("git", "fetch", "--quiet", "--unshallow", "origin")
    have(sha)
    run(
        "git",
        "fetch",
        "--quiet",
        "origin",
        f"+refs/heads/{base}:refs/remotes/origin/{base}",
    )

    subject = run("git", "log", "-1", "--format=%s", sha).strip()
    if bumped(subject) is None:
        print(f"Not a release pull request: {subject!r}")
        output(release="false", version="")
        return

    version, _ = bump(
        sha, env["PAR_VERSION_FILE"], env["PAR_VERSION_PATTERN"], env["PAR_CHANGELOG"]
    )
    if not succeeds("git", "merge-base", "--is-ancestor", f"origin/{base}", sha):
        raise Refusal(
            f"rebase on {base} and push again: {base} has commits this lacks, and a"
            " release is made only from the tree the pull request's checks ran on"
        )
    print(f"Releases v{version} once merged")
    output(release="true", version=version)


def check(env) -> None:
    repo, sha = env["PAR_REPOSITORY"], env["PAR_SHA"]
    workflows = env["PAR_WORKFLOWS"].split()
    if not workflows:
        raise Refusal(
            "workflows names none; name those that must pass on the pull request"
        )
    jobs = required_jobs(env.get("PAR_JOBS", ""), workflows)
    have(sha)

    version, notes = bump(
        sha, env["PAR_VERSION_FILE"], env["PAR_VERSION_PATTERN"], env["PAR_CHANGELOG"]
    )
    untagged_elsewhere(f"v{version}", sha)
    released = succeeds(
        "gh", "release", "view", f"v{version}", "--repo", repo, retry=True
    )
    pr, head, runs = tested(repo, sha, workflows, jobs)

    notes_file = ""
    if notes:
        notes_file = str(
            Path(env.get("RUNNER_TEMP") or tempfile.gettempdir())
            / f"notes-v{version}.md"
        )
        Path(notes_file).write_text(notes + "\n")

    print(f"Releases v{version}, as #{pr} was tested on this tree")
    output(
        version=version,
        released=str(released).lower(),
        pr=pr,
        pr_head=head,
        runs=json.dumps(runs),
        notes_file=notes_file,
    )


def release(env) -> None:
    repo, sha, version = env["PAR_REPOSITORY"], env["PAR_SHA"], env["PAR_VERSION"]
    if not version:
        raise Refusal("version is empty; pass the version output of the check action")
    tag = f"v{version}"
    fields = {"version": version, "tag": tag}
    try:
        title = (env.get("PAR_TITLE") or "{tag}").format(**fields)
        dispatches = [
            [part.format(**fields) for part in line.split()]
            for line in env.get("PAR_DISPATCH", "").splitlines()
            if line.strip()
        ]
    except (KeyError, IndexError, ValueError) as exc:
        raise Refusal(f"title and dispatch may use only {{version}} and {{tag}}: {exc}")
    for workflow, *inputs in dispatches:
        if any("=" not in field for field in inputs):
            raise Refusal(f"dispatch of {workflow}: an input is not 'name=value'")
    untagged_elsewhere(tag, sha)

    if succeeds("gh", "release", "view", tag, "--repo", repo, retry=True):
        print(f"{tag} is released already")
    else:
        notes = env.get("PAR_NOTES_FILE", "")
        run(
            "gh", "release", "create", tag, "--repo", repo, "--target", sha, "--title", title,
            *(["--notes-file", notes] if notes else ["--generate-notes"]),
        )  # fmt: skip
        print(f"Released {tag}")

    assets = []
    for pattern in env.get("PAR_ASSETS", "").splitlines():
        if pattern.strip():
            found = sorted(glob.glob(pattern.strip()))
            if not found:
                raise Refusal(f"no file matches the asset {pattern.strip()!r}")
            assets += found
    if assets:
        run(
            "gh", "release", "upload", tag, "--repo", repo, "--clobber", *assets,
            retry=True,
        )  # fmt: skip
        print(f"Attached {' '.join(Path(a).name for a in assets)}")

    if env.get("PAR_MAJOR_TAG") == "true":
        major = f"v{version.split('.')[0]}"
        run("git", "push", "--quiet", "--force", "origin", f"{sha}:refs/tags/{major}")
        print(f"Moved {major} to {tag}")

    for workflow, *inputs in dispatches:
        fields = [arg for field in inputs for arg in ("-f", field)]
        run("gh", "workflow", "run", workflow, "--repo", repo, "--ref", tag, *fields)
        print(f"Dispatched {' '.join([workflow, *inputs])} on {tag}")

    output(tag=tag)


COMMANDS = {"ready": ready, "check": check, "release": release}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in COMMANDS:
        print(f"usage: {Path(argv[0]).name} {{{','.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    try:
        COMMANDS[argv[1]](os.environ)
    except Refusal as exc:
        print(f"::error::{exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"::error::{' '.join(exc.cmd)} failed: {exc.stderr.strip()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
