# Source extraction recipes

Concrete commands for pulling requirements from each source. Never print tokens or cookies.

## 1. Jira task

Use the `web-fetcher` skill (corporate SSL safe). Authorization is a personal token.

```bash
# Issue fields (summary, description, acceptance criteria, status, links)
./scripts/web_fetch \
  "https://jira.sberbank.ru/rest/api/2/issue/HRM-11330?fields=summary,description,status,issuelinks,labels,attachment" \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'Content-Type: application/json' \
  -o jira-issue.json
```

What to extract:
- `fields.summary`, `fields.description` — main requirement text.
- Acceptance criteria — often in description or a custom field; search the description for "AC", "Критерии приёмки", numbered lists.
- `fields.issuelinks` — related stories/bugs that add scope.
- `fields.attachment` — specs, mockups (note their presence; fetch if needed).

## 2. Pull Request

### Preferred: local git

If the branch is checked out, derive scope from the diff directly.

```bash
# Files changed vs target branch (usually master/develop)
git diff --name-status origin/master...HEAD

# Full diff for changed logic
git diff origin/master...HEAD

# Recent commit messages on the branch (often reference the JIRA-KEY)
git log --oneline origin/master..HEAD
```

Map changes → requirements:
- New/changed REST endpoint → contract + functional requirement.
- DTO/field added, made required, removed → contract + regression requirement.
- New config / feature flag → environment + toggle behavior requirement.
- Migration / schema change → data + backward-compatibility requirement.

### Fallback: Stash / Bitbucket REST (when branch is not local)

```bash
# PR overview
./scripts/web_fetch \
  "https://stash.sigma.sbrf.ru/rest/api/1.0/projects/HRPLATFORM/repos/<repo>/pull-requests/<PR_ID>" \
  -H 'Authorization: Bearer <TOKEN>' -o pr.json

# PR changed files
./scripts/web_fetch \
  "https://stash.sigma.sbrf.ru/rest/api/1.0/projects/HRPLATFORM/repos/<repo>/pull-requests/<PR_ID>/changes" \
  -H 'Authorization: Bearer <TOKEN>' -o pr-changes.json

# PR diff
./scripts/web_fetch \
  "https://stash.sigma.sbrf.ru/rest/api/1.0/projects/HRPLATFORM/repos/<repo>/pull-requests/<PR_ID>/diff" \
  -H 'Authorization: Bearer <TOKEN>' -o pr-diff.txt
```

## 3. Confluence — via MCP Atlassian

Prefer the MCP tool over HTTP. Example (page id from the page URL):

```text
mcp__Atlassian__confluence_get_page(page_id="17151460968", convert_to_markdown=true)
```

To find a page when only the title/space is known, use the MCP search tool, then fetch by id.

What to extract:
- Business rules and process descriptions → functional requirements.
- Tables of states/permissions → state-transition and authorization cases.
- Non-functional notes (limits, timeouts, volumes) → NFR requirements.

Fallback over HTTP (only if MCP is unavailable):

```bash
./scripts/web_fetch \
  "https://confluence.sberbank.ru/rest/api/content/17151460968?expand=body.storage" \
  -H 'Authorization: Bearer <TOKEN>' -o confluence-page.json
```

## Merge order

When sources disagree, record the contradiction (do not silently pick one):
1. **Code/PR** = what is actually implemented (ground truth for current behavior).
2. **Jira** = what was requested.
3. **Confluence** = broader business context / rules.

A mismatch between (1) and (2)/(3) is itself a finding to report.
