# GitHub release tracker

Watches a list of GitHub repositories for new releases and merged pull requests,
writes a short post about each one, and holds it for approval before publishing
to X.

Approval happens over plain HTTP, so any front end can drive it. The bundled
webhook is what the DeepSeek Harness plugin uses to list drafts in the dashboard,
but the API stands on its own.

Everything runs as a single n8n container.

## How it works

The workflow has one schedule trigger and one webhook trigger.

Every two minutes it drains the approval queue. Every fifteen minutes it polls
GitHub, so new items never wait longer than one poll interval.

- A published release that is not a draft or a prerelease becomes a draft post
  immediately.
- Merged pull requests are queued and written up as one "new in <project>" post
  on a digest interval. A busy week does not flood the timeline.
- A manual note can be queued for anything the tracker does not watch.

Drafts come from an OpenAI-compatible endpoint. Without a key the tracker writes
a plain templated draft instead and still asks for approval.

Approving calls `POST /2/tweets` with OAuth 1.0a user context. The signature is
computed inside the workflow, which means no public callback URL and no OAuth
credential in n8n.

## Requirements

- Docker with the Compose plugin
- A GitHub token with read access to the tracked repositories
- An X developer app with Read and write permission, if you want to publish
- An OpenAI-compatible API key, if you want written drafts

## Setup

```bash
git clone https://github.com/emiliano-go/github-release-tracker.git
cd github-release-tracker
cp .env.example .env
# fill in N8N_ENCRYPTION_KEY, GITHUB_TOKEN, DSH_WEBHOOK_TOKEN
./install.sh
```

`install.sh` starts the stack, imports the workflow, publishes it, and restarts
n8n. Running it again after an edit is safe: the workflow keeps a stable id, so
the import updates it in place.

The editor listens on `127.0.0.1:5678` by default. Set `N8N_BIND_IP` and
`N8N_HOST` in `.env` if you want it reachable from the LAN; the port is never
exposed to the internet by the stack.

Open the editor and create the owner account on first run.

### GitHub token

A fine-grained token scoped to the tracked repositories needs:

- Contents: Read
- Pull requests: Read
- Metadata: Read

A classic token with `public_repo` covers public repositories. Releases and pull
requests both work read-only; the tracker no longer needs webhook or admin
rights.

### X credentials

Create the app as "Web app, automated app or bot", set permissions to Read and
write, and only then generate the access token. A token generated while the app
was read-only cannot post.

Four values go in `.env`: consumer key and secret from the app keys, access token
and secret from the "Keys and tokens" page. The bearer token is not used, because
app-only auth cannot post.

Set `X_ENABLED=true` to let approvals publish.

## Approval API

Every request is a `POST` with the shared `x-totem-token` header.

| Body | Result |
| --- | --- |
| `{"op":"list"}` | Pending drafts with their text and source |
| `{"op":"act","key":"r1abc","action":"ok\|re\|no"}` | Queue a post, re-draft, or skip |
| `{"op":"enqueue","note":"...","url":"https://..."}` | Draft a manual post now |

```bash
curl -s http://localhost:5678/webhook/totem-approvals \
  -H "content-type: application/json" \
  -H "x-totem-token: $DSH_WEBHOOK_TOKEN" \
  -d '{"op":"list"}'
```

Actions are queued and applied on the next cycle, so an approval can take up to
two minutes to reach X. Manual notes are drafted inside the request and appear in
the list straight away.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GITHUB_TOKEN` | none | GitHub API token |
| `TRACKED_REPOS` | see `.env.example` | Comma-separated `owner/name` |
| `GITHUB_POLL_MINUTES` | 15 | Poll interval, minimum 5 |
| `DIGEST_HOURS` | 24 | How often merged PRs are summarised |
| `OPENAI_API_KEY` | empty | Draft writing |
| `OPENAI_BASE_URL` | Gemini OpenAI endpoint | Any compatible endpoint |
| `OPENAI_MODEL` | `gemini-3.5-flash` | Model id |
| `X_ENABLED` | false | Gate on publishing |
| `X_CONSUMER_KEY` / `X_CONSUMER_SECRET` | empty | App keys |
| `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | empty | User context |
| `DSH_WEBHOOK_TOKEN` | empty | Shared secret for the approval API |
| `N8N_ENCRYPTION_KEY` | none | Encrypts n8n credentials |

State lives in n8n's workflow static data inside `./data`: seen releases, the PR
queue, pending drafts, and the action queue. The first run records what already
exists, so installing this does not announce history.

## Notes

Gemini's free tier is enough for this workload: a few calls a day against limits
of roughly ten requests per minute. `gemini-3.8-flash` and `gemini-3.7-flash`
return 503 under load often enough to be useless here, and `gemini-2.5-flash`
answers 404 for newer projects. `gemini-3.5-flash` held up.

Drafts are queued until you act on them. Nothing expires, so an ignored draft
stays in the list.

`X_ENABLED=false` does not stop detection. Approving returns a hint instead of
publishing, so you can run the tracker for a while before wiring X.

The repository is MIT licensed. See `LICENSE`.

## Development

`build_workflow.py` generates `workflows/github-release-tracker.json`, and
`test_workflow.js` checks the Code nodes against a stubbed n8n runtime.

```bash
python3 build_workflow.py
node test_workflow.js
./install.sh
```

Edit the generator rather than the JSON. The JSON is rewritten on every build.
