# buffer-mcp

[![CI](https://github.com/jakemeany523/buffer-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/jakemeany523/buffer-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

MCP server for Buffer social media scheduling via the GraphQL API. Works with Claude Desktop, Claude Cowork, and any MCP-compatible AI tool.

The legacy REST API (`api.bufferapp.com/1/`) is deprecated. This server uses Buffer's current GraphQL API exclusively.

## Setup

### 1. Install dependencies

```bash
cd buffer-mcp
bash install.sh
```

Or manually:

```bash
pip install mcp httpx pydantic
```

### 2. Get your Buffer credentials

- **Access token**: Buffer → Settings → API (or Settings → Beta)
- **Org ID**: Run `buffer_get_account` after connecting — use the org ID from `account.organizations`, NOT `account.id` (they differ and using the wrong one causes silent failures)
- **Channel IDs**: Run `buffer_list_channels` to get your Twitter/LinkedIn channel IDs

### 3. Configure environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

### 4. Add to Claude Desktop / Cowork

Add to your MCP config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "buffer": {
      "command": "python3",
      "args": ["/absolute/path/to/buffer-mcp/server.py"],
      "env": {
        "BUFFER_ACCESS_TOKEN": "your-token-here",
        "BUFFER_ORG_ID": "your-org-id-here",
        "BUFFER_CHANNEL_TWITTER": "your-twitter-channel-id",
        "BUFFER_CHANNEL_LINKEDIN": "your-linkedin-channel-id"
      }
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `buffer_list_channels` | List connected social channels with IDs |
| `buffer_get_account` | Get account info and find your org ID |
| `buffer_create_post` | Schedule a single post (with images, videos, threads, LinkedIn metadata) |
| `buffer_batch_create_posts` | Schedule up to 25 posts in one call |
| `buffer_update_post` | Edit a scheduled post (atomic delete + recreate) |
| `buffer_delete_post` | Remove a post from the queue |
| `buffer_find_post_by_schedule` | Look up the canonical ID for a post by its scheduled time |
| `buffer_list_posts` | List scheduled posts in your queue |
| `buffer_list_published_posts` | List posts that have already been sent |
| `buffer_get_engagement` | Pull engagement metrics for a published post (Twitter wired; LinkedIn requires Marketing API) |
| `buffer_get_reply_links` | Fetch recent tweet URLs for reply/thread follow-up |
| `buffer_upload_image` | Upload a local image or video to Cloudflare R2 and return a Buffer-safe URL |
| `buffer_health_check` | Verify token validity and API connectivity |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `BUFFER_ACCESS_TOKEN` | Yes | Your Buffer API access token |
| `BUFFER_ORG_ID` | Yes | Your Buffer organization ID (from `buffer_get_account`) |
| `BUFFER_CHANNEL_TWITTER` | No | Twitter channel ID — enables `"twitter"` shorthand |
| `BUFFER_CHANNEL_LINKEDIN` | No | LinkedIn channel ID — enables `"linkedin"` shorthand |
| `BUFFER_TWITTER_USERNAME` | No | Your Twitter username for `buffer_get_reply_links` |
| `TWITTER_BEARER_TOKEN` | No | Twitter API v2 bearer token (for reply links + engagement) |
| `CLOUDFLARE_ACCOUNT_ID` | No | Cloudflare account ID (for `buffer_upload_image`) |
| `CLOUDFLARE_R2_API_TOKEN` | No | R2 API token with Object:Write permission |
| `CLOUDFLARE_R2_BUCKET_NAME` | No | R2 bucket name (default: `buffer-media`) |
| `CLOUDFLARE_R2_PUBLIC_BASE_URL` | No | Public CDN URL for your R2 bucket |

## Key Notes

### Media hosting
Buffer's media fetcher rejects many free CDNs (catbox, imgur return 0-byte or 429 for Buffer's `node-fetch` UA). Use Cloudflare R2, AWS S3, or another persistent CDN. The `buffer_upload_image` tool handles R2 uploads automatically when configured.

### Twitter threads
To post a thread, use `thread_replies` in `buffer_create_post`. Twitter suppresses posts with links in the body — put your CTA URL in the first reply, not the parent tweet.

### Post ID rotation (known Buffer bug)
`createPost` can return a transient ID that differs from the ID returned by `buffer_list_posts` for the same post. Always use `buffer_find_post_by_schedule` to get the canonical (deletable) ID before calling `buffer_delete_post` or `buffer_update_post`.

### Org ID vs Account ID
Buffer exposes both `account.id` (account-level) and `account.organizations[].id` (org-level). Using the account ID as the org ID causes silent `"Organization not found"` errors. Run `buffer_get_account` and copy the org ID from the organizations list.

## Development

```bash
# Install with dev dependencies (pytest, ruff)
pip install -e ".[dev]"

# Run the test suite
pytest -q

# Lint
ruff check .
```

CI runs the tests and linter on every push and pull request across Python
3.10–3.12 (see `.github/workflows/ci.yml`). See [CHANGELOG.md](CHANGELOG.md)
for release history.

## Contributing

Issues and pull requests are welcome. Please run `ruff check .` and `pytest`
before opening a PR, and add tests for any new tools or behavior changes.

## License

[MIT](LICENSE)
