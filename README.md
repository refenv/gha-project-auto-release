# gha-project-auto-release

[![test](https://github.com/refenv/gha-project-auto-release/actions/workflows/test.yml/badge.svg)](https://github.com/refenv/gha-project-auto-release/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/refenv/gha-project-auto-release?sort=semver)](https://github.com/refenv/gha-project-auto-release/releases)
[![license](https://img.shields.io/github/license/refenv/gha-project-auto-release)](LICENSE)
[![REUSE status](https://api.reuse.software/badge/github.com/refenv/gha-project-auto-release)](https://api.reuse.software/info/github.com/refenv/gha-project-auto-release)

Release a project when a commit with the subject `ver: bump to v<version>`
lands on its main branch, from the tree its pull request's checks ran on.

```yaml
name: release

on:
  push:
    branches: [main]

concurrency:
  group: release
  cancel-in-progress: false

jobs:
  release:
    if: >-
      startsWith(github.event.head_commit.message, 'ver: bump to v')
    runs-on: ubuntu-latest
    permissions:
      actions: read
      contents: write
      pull-requests: read
    steps:
    - uses: actions/checkout@v7
    - id: check
      uses: refenv/gha-project-auto-release/check@v1
      with:
        version-file: src/foo/__init__.py
        version-pattern: '^__version__ = "([^"]+)"$'
        workflows: verify.yml
    - uses: refenv/gha-project-auto-release/release@v1
      with:
        version: ${{ steps.check.outputs.version }}
        notes-file: ${{ steps.check.outputs.notes-file }}
```

A release is made from a pull request whose last commit bumps the version.
When it is merged, the release job checks that the bump is complete, that the
pull request's head has the tree main now has, and that the named workflows
passed on that head. Only then does it tag the commit and create the GitHub
Release. What the project publishes, to PyPI, crates.io or a container
registry, goes between the two steps, and can use the build the pull request
tested: the `runs` output names the run to download its artifacts from.

A failed or cancelled release job can be run again. A tag of the same commit
and a release that exists are taken as done; a tag of another commit fails
the job.

## The actions

There are three, one per directory, sharing `project_auto_release.py`. They
need `python3`, `git` and `gh`, as GitHub's runner images have, and run git in
a checkout of the repository being released.

### ready

On a pull request whose last commit is a version bump, it checks that the bump
is complete and that the branch has every commit of its base. Without the
latter, the checks run on a tree that is not the one main will have, and the
release job fails after the merge. On any other pull request it outputs
`release: false` and checks nothing, so a workflow can use that output to run
what only a release needs.

```yaml
- uses: actions/checkout@v7
- id: ready
  uses: refenv/gha-project-auto-release/ready@v1
  with:
    version-file: src/foo/__init__.py
    version-pattern: '^__version__ = "([^"]+)"$'
```

| Input             | Default          | Description |
|-------------------|------------------|-------------|
| `version-file`    |                  | The file that carries the version |
| `version-pattern` |                  | A Python regular expression whose first group captures the version in `version-file` |
| `changelog`       | `CHANGELOG.md`   | The changelog that must have a `## <version>` section; empty requires none |
| `sha`             | the head commit  | The pull request's head commit |
| `base`            | `github.base_ref`| The branch the pull request merges into |

Outputs: `release`, `true` or `false`, and `version`.

### check

On main, it checks that the commit is a complete version bump: its subject
names the version, `version-file` carries it, and `changelog` has a section
for it. It then requires that the pull request that merged the commit has the
same tree, and finds a successful run of each of `workflows` on its head, in
which each of `jobs` concluded success. Only a run for the pull request
counts, not a run of the same commit dispatched by hand or pushed to a branch.
A job that was skipped has not passed, so a job behind a condition, such as a
hardware test opened by a label, must have run.

| Input             | Default            | Description |
|-------------------|--------------------|-------------|
| `version-file`    |                    | As for `ready` |
| `version-pattern` |                    | As for `ready` |
| `changelog`       | `CHANGELOG.md`     | The changelog whose `## <version>` section is the release's notes; empty lets GitHub generate them |
| `workflows`       |                    | Workflow files that must have a successful run on the pull request's head, separated by spaces |
| `jobs`            |                    | Jobs that must have concluded success in those runs, one per line as `<workflow>: <job name>`; a name matches each job of a matrix, `name (...)`, and each job of a reusable workflow it calls, `name / ...` |
| `token`           | `github.token`     | The token for the GitHub API |
| `repository`      | `github.repository`| The repository, as owner/name |
| `sha`             | `github.sha`       | The commit to release |

| Output       | Description |
|--------------|-------------|
| `version`    | The version, such as `1.2.3` |
| `released`   | Whether its GitHub Release exists already, `true` or `false` |
| `pr`         | The number of the pull request that merged the commit |
| `pr-head`    | The pull request's head commit |
| `runs`       | The run of each workflow on `pr-head`, as JSON, such as `{"verify.yml": 123}` |
| `notes-file` | The file holding the section of `changelog`, or empty |

To publish the build the pull request tested:

```yaml
- env:
    GH_TOKEN: ${{ github.token }}
    RUN: ${{ fromJSON(steps.check.outputs.runs)['verify.yml'] }}
  run: gh run download "$RUN" --name dist --dir dist
```

### release

It tags the commit `v<version>`, creates the GitHub Release unless it exists,
attaches `assets`, moves the major tag when asked, and dispatches workflows on
the tag. A tag created with the workflow's token triggers no push event, so a
workflow that publishes on a `v*` tag must be dispatched to run on it, and must
accept `workflow_dispatch`.

| Input        | Default            | Description |
|--------------|--------------------|-------------|
| `version`    |                    | The `version` output of `check` |
| `notes-file` |                    | The `notes-file` output of `check`; empty lets GitHub generate the notes |
| `title`      | `{tag}`            | The release's title; `{version}` and `{tag}` are replaced |
| `assets`     |                    | Files to attach, one path or glob per line; an attachment of the same name is replaced |
| `major-tag`  | `false`            | Move the tag of the major version, such as `v1`, to the release |
| `dispatch`   |                    | Workflows to run on the tag, one per line as the file and its inputs, such as `verify.yml job=docgen`; `{version}` and `{tag}` are replaced |
| `token`      | `github.token`     | The token for the GitHub API |
| `repository` | `github.repository`| The repository, as owner/name |
| `sha`        | `github.sha`       | The commit to release |

Output: `tag`, such as `v1.2.3`.

`dispatch` needs the `actions: write` permission, and `major-tag` pushes a tag
with the checkout's credentials.

## Merging a release pull request

The release job runs on the commit at the head of main after the merge, and
only when its subject is the bump. Merge by rebase, or squash with the pull
request titled as the bump; a merge commit has another subject. The pull
request must have every commit of main when merged, which `ready` checks and
branch protection's "require branches to be up to date" enforces.
