# RepoPages CI client

Docs as code for Confluence. The client side of [RepoPages for Confluence](https://marketplace.atlassian.com/): your CI runs it on every push, it signs the Markdown files that changed and sends them to RepoPages, which writes one Confluence page per file. Mermaid and PlantUML blocks are rendered here, on your runner, and sent as SVG. Nothing in Confluence ever connects to your Git host. That makes private, self-hosted and VPN-only repositories work without any OAuth grant or GitHub app: the only thing that reads the repository is your own CI.

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

## Edits made in Confluence

Git stays the source of truth, but people who live in Confluence can still change a page. When someone edits a synced page, RepoPages does not throw the edit away and does not write to Git. It marks the page instead:

- the byline chip changes to **RepoPages · pending review**, and its popup says the edit is waiting to become a merge request;
- the page gets the label `repopages-pending` and an attachment `repopages-pending.md` with the proposed Markdown file;
- if the edit uses something the converter cannot turn back into Markdown yet (tables, images, most macros), the chip says **not sent** and why. The edit stays in the page history and nothing goes to Git.

A job in **your** CI then picks the edit up: `repopages_push.py pull-edits` reads the pending pages with a read-only Confluence token, pushes one branch per edit and opens a pull request (GitLab: merge request) with the editor's name in it, then tells RepoPages the link. The chip changes to **RepoPages · merge request** and links to it. Review and merge as usual; the push of the merge rewrites the page from Git and the pending mark disappears. RepoPages itself never calls GitHub or GitLab and holds no credential for your repository; everything that touches Git runs on your runner, with your runner's permissions.

What the pull request contains:

- branch `repopages/edit-<page id>-v<page version>`, one commit `Confluence edit: <path>` by `RepoPages <noreply@repopages.app>` (change it with `REPOPAGES_GIT_AUTHOR="Docs Bot <docs@acme.com>"`), with the trailers `Edited-in-Confluence-by: <name>` and `Confluence-page: <link>`;
- if the file changed in Git since the page was last synced, the edit is merged onto the current version with `git merge-file`. Overlapping changes are left as conflict markers and the pull request gets the label `needs-attention`. Nothing is dropped silently;
- branches are never force-pushed. A page edited again gets a new branch for its new version.

### When it runs

Nothing in Confluence can start your CI on its own, so pick one or more:

| Trigger | Delay | Needs |
|---|---|---|
| Run the workflow by hand (`workflow_dispatch`, GitLab "Run pipeline") | none | nothing |
| Schedule, hourly (`0 * * * *`) or nightly | up to the interval | nothing; an empty run takes about 30 s of CI time, so every 15 minutes would use over half of a private repository's free GitHub minutes |
| A Confluence Automation rule: label `repopages-pending` added → Send web request to `repository_dispatch` or a GitLab pipeline trigger | about a minute | Confluence Premium or Enterprise, and a token stored in the rule ([how](examples/confluence-automation-rule.md)) |

### The Confluence token

The job reads Confluence with an Atlassian API token of an account that can see the mapped spaces (a service account is best). Create it at **id.atlassian.com → Security → API tokens**. A scoped token is recommended; it needs only

- `read:page:confluence`
- `read:attachment:confluence`
- `search:confluence`

A classic (unscoped) token works too. The client tries `https://<site>/wiki` first and switches to `https://api.atlassian.com/ex/confluence/<cloud id>` (which scoped tokens require) when the site answers 401 or 403; `--scoped-token` goes there directly. The token never leaves your CI and is never printed.

### GitHub Actions

```yaml
# .github/workflows/repopages-pull-edits.yml
name: RepoPages edits
on:
  schedule:
    - cron: '0 * * * *'
  workflow_dispatch:
permissions:
  contents: write
  pull-requests: write
jobs:
  pull-edits:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: repopages-app/ci@v1
        with:
          mode: pull-edits
          url: ${{ secrets.REPOPAGES_URL }}
          secret: ${{ secrets.REPOPAGES_SECRET }}
          confluence-site: https://acme.atlassian.net
          confluence-email: ${{ secrets.CONFLUENCE_EMAIL }}
          confluence-token: ${{ secrets.CONFLUENCE_TOKEN }}
```

Also turn on **Settings → Actions → General → Allow GitHub Actions to create and approve pull requests**, or opening the pull request fails with HTTP 403. Pull requests opened with the workflow's token do not start other workflows (a GitHub rule); push to the branch or close and reopen the pull request to run your checks. The full file, with the Automation trigger, is [`examples/github-actions-pull-edits.yml`](examples/github-actions-pull-edits.yml); the GitLab job is at the end of [`examples/gitlab-ci.yml`](examples/gitlab-ci.yml) and needs a project access token (`GITLAB_TOKEN`, scopes `api` and `write_repository`).

Inputs for `mode: pull-edits` (besides `url` and `secret`):

| Input | Default | Meaning |
|---|---|---|
| `confluence-site` | required | `https://acme.atlassian.net` |
| `confluence-email`, `confluence-token` | required | the token's account and the token, as secrets |
| `confluence-space` | every space | only pages in this space key |
| `base` | the default branch | the branch pull requests target |
| `mr` | `github` | `none` pushes the branches only and prints the pull requests to open |
| `args` | `''` | extra flags, e.g. `--dry-run` or `--no-ack` |

### From the command line

```sh
export CONFLUENCE_SITE=https://acme.atlassian.net CONFLUENCE_EMAIL=bot@acme.com CONFLUENCE_TOKEN=...
python3 repopages_push.py pull-edits --repo owner/name --dry-run    # show what would be opened; changes nothing
```

`--dry-run` lists the pending pages, what each proposal contains and which branch and pull request it would create; it pushes, opens and sends nothing. Other flags: `--space KEY`, `--base BRANCH`, `--branch-prefix` (default `repopages/edit`), `--mr github|gitlab|none` (default: `github` on GitHub Actions, `gitlab` on GitLab CI, else `none`), `--no-ack`, `--scoped-token`, `--confluence-token-file`.

Exit codes of `pull-edits`: `0` done (a malformed attachment is a warning, the page is skipped); `1` Confluence could not be read; `2` configuration error (site, email, token, repository, or the sync URL and secret missing) or Confluence refused the token; `3` RepoPages did not accept the report of the opened pull requests (check `REPOPAGES_URL` and `REPOPAGES_SECRET`); `4` a branch could not be pushed or a pull request could not be opened (the next run retries it).

## Versions

Tags follow semver. `v1` moves with every 1.x release, so `uses: repopages-app/ci@v1` and `ghcr.io/repopages-app/ci:1` pick up fixes; pin `@v1.0.0` or `:1.0.0` for a fixed version. The client and the app share a payload contract; a new major version of the client is only needed when that contract changes.

## License

MIT, see [LICENSE](LICENSE).
