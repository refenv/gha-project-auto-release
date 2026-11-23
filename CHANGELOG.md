# Changelog

Each release has a section here, named by its version, such as `## 1.0.0`;
that section is the text of its release on GitHub.

## 1.0.2

A request to GitHub that fails with an HTTP 5xx is repeated after 5, 15 and 45
seconds before the job fails, where repeating it is safe: every read, and
attaching assets. Creating the release and dispatching a workflow are not
repeated.

## 1.0.1

`check` takes only a pull_request run of each workflow on the pull request's
head. It took the newest successful run of any event, so a run of the same
commit dispatched by hand could be released from in place of the pull
request's own. The README's example workflow parses: its `if:` is now a
folded scalar.

## 1.0.0

The first release. Three actions release a project when a commit with the
subject `ver: bump to v<version>` lands on its main branch. `ready` checks a
release pull request before it is merged: that the bump is complete and the
branch is not behind its base. `check` requires, on main, that the pull
request that merged the bump has the same tree, and that the named workflows,
and jobs within them, passed on its head. `release` tags the commit, creates
the GitHub Release, attaches assets, moves the major tag, and dispatches
workflows on the tag.
