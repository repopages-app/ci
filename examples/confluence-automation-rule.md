# Start the pull-edits job right after an edit (Confluence Premium)

Without this, edits made in Confluence reach Git on the job's schedule (hourly in the examples). On
Confluence **Premium** or **Enterprise**, an Automation rule can start the job about a minute after
someone edits a page. The request comes from your own Automation rule with a token you keep there;
RepoPages itself still never calls your Git host.

## The rule

**Space settings → Automation** (one space) or **Confluence settings → Automation** (global) → *Create rule*:

1. **Trigger:** label added to a page. **Condition:** the label is `repopages-pending`.
2. **Action: Send web request**

   GitHub (`repository_dispatch`):

   | Field | Value |
   |---|---|
   | URL | `https://api.github.com/repos/OWNER/REPO/dispatches` |
   | Method | `POST` |
   | Headers | `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Authorization: Bearer <token>` (mark it hidden) |
   | Body | Custom data: `{"event_type": "repopages-pull-edits"}` |

   The token: a fine-grained personal access token for that one repository with **Contents: read and
   write** (what `repository_dispatch` requires), or a GitHub App token.

   GitLab (pipeline trigger token, *Settings → CI/CD → Pipeline trigger tokens*):

   | Field | Value |
   |---|---|
   | URL | `https://gitlab.com/api/v4/projects/<project id>/trigger/pipeline` |
   | Method | `POST` |
   | Body | Form: `token=<trigger token>&ref=main&variables[REPOPAGES_JOB]=pull-edits` |

3. Name it "RepoPages: propose Confluence edits" and turn it on.

## The matching trigger in the workflow

GitHub, in `.github/workflows/repopages-pull-edits.yml` (already in
[github-actions-pull-edits.yml](github-actions-pull-edits.yml)):

```yaml
on:
  repository_dispatch:
    types: [repopages-pull-edits]
```

GitLab: the `repopages-pull-edits` job in [gitlab-ci.yml](gitlab-ci.yml) already runs for
`$CI_PIPELINE_SOURCE == "trigger"` with `REPOPAGES_JOB=pull-edits`.

## Keep the schedule

The label is only *added* by the first edit; a second edit to a page that is still pending changes the
proposal but adds no label, so the rule does not fire. The hourly or nightly schedule picks those up.
Each rule run counts against your site's Automation usage.
