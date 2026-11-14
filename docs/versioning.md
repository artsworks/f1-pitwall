# Versioning

The project follows [Semantic Versioning 2.0.0](https://semver.org). While the version is 0.x, a breaking change bumps MINOR instead of MAJOR. Moving to 1.0.0 is a manual decision.

Pull requests are squash-merged, so the PR title becomes the commit on `main`. The PR title must be a [Conventional Commit](https://www.conventionalcommits.org), and CI checks it. On each push to `main`, the release workflow reads the title and bumps the version:

| PR title | Bump on 0.x |
|---|---|
| `fix: ...`, `perf: ...` | PATCH |
| `feat: ...` | MINOR |
| `feat!: ...` or a `BREAKING CHANGE:` footer | MINOR |
| `docs:`, `chore:`, `refactor:`, `test:`, `build:`, `ci:` | none |

The workflow updates `pyproject.toml` and `uv.lock`, adds an entry to [`CHANGELOG.md`](../CHANGELOG.md), and pushes a `vX.Y.Z` tag, then creates a GitHub Release with the same notes. Do not edit the version by hand.
