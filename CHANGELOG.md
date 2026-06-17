# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- GitHub Actions CI: runs the test suite and `ruff` lint on Python 3.10–3.12.
- `LICENSE` file (MIT) to match the declared license.
- `dev` optional-dependency group (`pytest`, `ruff`) and pytest/ruff config.
- `main()` console-script entry point and a hatch wheel target for `server.py`.

### Fixed
- Corrected the `buffer-mcp` console-script entry point (`server:main`), which
  previously pointed at a non-callable (`server:mcp.run`).
- Resolved lint issues (unused imports, multi-statement lines, empty f-strings).

## [2.0.0] - 2026-06-17

### Added
- Initial release: Buffer MCP server over the GraphQL API with 13 tools
  (channels, account, create/update/delete/list posts, batch create,
  find-by-schedule, published posts, engagement, reply links, media upload,
  health check).
- Canonical post-ID resolution to work around Buffer's transient ID rotation.
- Paginated post listing with legacy-query fallback.
- Cloudflare R2 media upload helper.
- `unittest` suite covering helpers, pagination, canonical-ID, and delete flows.
