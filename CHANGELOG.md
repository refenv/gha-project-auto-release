# Changelog

Each release has a section here, named by its version, such as `## 1.0.0`;
that section is the text of its release on GitHub.

## 1.0.0

The first release. Three actions release a project when a commit with the
subject `ver: bump to v<version>` lands on its main branch. `ready` checks a
release pull request before it is merged: that the bump is complete and the
branch is not behind its base. `check` requires, on main, that the pull
request that merged the bump has the same tree, and that the named workflows,
and jobs within them, passed on its head. `release` tags the commit, creates
the GitHub Release, attaches assets, moves the major tag, and dispatches
workflows on the tag.
