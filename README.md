# RepoPages CI client

The client side of [RepoPages for Confluence](https://marketplace.atlassian.com/): your CI runs it on every push, it signs the Markdown files that changed and sends them to RepoPages, which writes one Confluence page per file. Mermaid and PlantUML blocks are rendered here, on your runner, and sent as SVG. Nothing in Confluence ever connects to your Git host.

Three ways to run it:

| | Use when | What you add |
|---|---|---|
| [GitHub Action](#github-actions) `repopages-app/ci@v1` | the docs live on GitHub | one workflow file |
| [Docker image](#gitlab-ci-and-other-runners-docker-image) `ghcr.io/repopages-app/ci` | GitLab CI, Jenkins, Bitbucket Pipelines, any runner without Chrome or Java | one job using the image |
| [`repopages_push.py`](repopages_push.py) | anything with Python 3 and git | one file, no dependencies |

Before any of them: map the repository on **Confluence settings → RepoPages**. That gives you the sync URL and, once, the signing secret. Put both in your CI's secret store as `REPOPAGES_URL` and `REPOPAGES_SECRET`. The settings page also shows these snippets filled in for each mapping.

## GitHub Actions

```yaml
# .github/workflows/repopages.yml
name: RepoPages
on:
  push:
    branches: [main]
    paths: ['**/*.md', '**/*.markdown']
  workflow_dispatch:
    inputs:
      full_import:
        description: 'Send every Markdown file (unchanged pages are skipped)'
        type: boolean
        default: false
jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 2   # push mode diffs HEAD~1..HEAD
      - uses: repopages-app/ci@v1
        with:
          url: ${{ secrets.REPOPAGES_URL }}
          secret: ${{ secrets.REPOPAGES_SECRET }}
          prefix: docs/    # the mapping's folder; leave empty for the whole repository
          mode: ${{ inputs.full_import && 'import' || 'push' }}
```

The first run is a full import: open the Actions tab, **RepoPages → Run workflow**, tick `full_import`. After that every push to `main` sends only the files the commit changed. [`examples/github-actions.yml`](examples/github-actions.yml) adds a `force` input that rewrites every page.

Inputs:

| Input | Default | Meaning |
|---|---|---|
| `url`, `secret` | required | sync URL and signing secret from the settings page, as repository secrets |
| `mode` | `push` | `push` sends the files changed by the commit; `import` sends the whole tree in two passes, so links between pages resolve |
| `prefix` | `''` | the folder the mapping covers, with a trailing slash |
| `force` | `false` | rewrite pages even when the file is unchanged |
| `exclude` | `''` | comma-separated globs to leave out, e.g. `CLAUDE.md,drafts/**` |
| `render` | `auto` | `off` sends diagram blocks as code and needs no renderer |
| `args` | `''` | extra client flags, e.g. `--allow-partial` or `--allow-missing-renderer` |
| `mermaid-cli`, `plantuml-version`, `plantuml-sha256` | pinned | the renderers; the runner's Chrome and Java are used, the PlantUML jar is cached |

Diagrams render on `ubuntu-latest` without any setup. A renderer that is missing on another runner fails the job with exit 2 and nothing sent, so a broken runner can never strip the images from pages; set `render: off` or `args: --allow-missing-renderer` to send such blocks as code instead.

## GitLab CI and other runners (Docker image)

`ghcr.io/repopages-app/ci` has the client, the Mermaid CLI with Chromium and PlantUML with a headless JRE. Tags: `1` (latest 1.x), `1.0.0`, `latest`.

```yaml
# .gitlab-ci.yml  (CI/CD variable, masked and protected: REPOPAGES_SECRET)
repopages:
  image: ghcr.io/repopages-app/ci:1
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
      changes: ['**/*.md', '**/*.markdown']
    - if: $CI_PIPELINE_SOURCE == "web"   # Run pipeline with FULL_IMPORT=true for a full import
  variables:
    GIT_DEPTH: "2"
    DOCS_PREFIX: "docs/"
    REPOPAGES_URL: "<sync URL>"
  script:
    - export GITHUB_SERVER_URL="$CI_SERVER_URL" GITHUB_REF="refs/heads/$CI_COMMIT_BRANCH"
    - if [ "$FULL_IMPORT" = "true" ]; then MODE=import; else MODE=push; fi
    - repopages "$MODE" --repo "$CI_PROJECT_PATH"
```

`--repo` must be the repository exactly as mapped (`owner/name`, GitLab `group/project`). The same image works anywhere Docker runs:

```sh
docker run --rm -v "$PWD:/repo" -w /repo -e REPOPAGES_URL -e REPOPAGES_SECRET \
  ghcr.io/repopages-app/ci:1 repopages push --repo owner/name --prefix docs/
```

## The script

[`repopages_push.py`](repopages_push.py) is a single stdlib-only Python 3 file. Copy it next to your pipeline, or run it from a checkout of this repository:

```sh
export REPOPAGES_URL='<sync URL>' REPOPAGES_SECRET='<secret>'
python3 repopages_push.py push   --repo owner/name --prefix docs/      # the files changed by HEAD
python3 repopages_push.py import --repo owner/name --prefix docs/      # the whole tree, first time
python3 repopages_push.py push   --dry-run --repo owner/name docs/a.md  # show the payload, send nothing
python3 repopages_push.py selftest                                      # offline checks, no network
```

[`examples/any-ci.sh`](examples/any-ci.sh) is the same with a `curl` connection test first. `python3 repopages_push.py --help` lists every flag; the main ones:

- Content is read from git at `--rev` (default `HEAD`), not from the working tree, so what is sent is what was committed.
- `--exclude GLOB` (repeatable, or comma-separated): a pattern without `/` matches a file name at any depth (`CLAUDE.md`), one with `/` matches the repository path (`drafts/**`). The mapping's own include and exclude patterns in Confluence always apply on top.
- `--render auto|off`, `--diagram-format svg|png|both`, `--diagram-theme neutral|light|dark`, `--diagram-background COLOR`. Mermaid needs `mmdc` on `PATH` or `MERMAID_CLI` set to a command; PlantUML needs `PLANTUML_JAR` with `java`, or `plantuml` on `PATH`.
- Code includes in the FastAPI style, `{* ../../docs_src/app.py ln[1:9] *}`, are expanded from the repository before sending.
- Calls carry at most 15 files and 4 MB; a run sends them one after another and retries once on 429 or 5xx.

Exit codes: `0` everything accepted, or nothing to send; `1` a call failed after the retry; `2` configuration or renderer error, nothing sent; `3` every chunk was sent but some files failed on the Confluence side (HTTP 207; the settings page lists them under Manage → Last run). `--allow-partial` turns 3 into a warning.

## Versions

Tags follow semver. `v1` moves with every 1.x release, so `uses: repopages-app/ci@v1` and `ghcr.io/repopages-app/ci:1` pick up fixes; pin `@v1.0.0` or `:1.0.0` for a fixed version. The client and the app share a payload contract; a new major version of the client is only needed when that contract changes.

## License

MIT, see [LICENSE](LICENSE).
