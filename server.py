#!/usr/bin/env python3
"""
MCP Server for Buffer Social Media Management (GraphQL API).

Provides tools to schedule, manage, and delete social media posts through
Buffer's GraphQL API at api.buffer.com. Supports Twitter/X, LinkedIn, and
other connected channels with image attachments and custom scheduling.

The legacy REST API (api.bufferapp.com/1/) is deprecated and returns 500s.
This server uses the current GraphQL API exclusively.

Configuration (environment variables):
    BUFFER_ACCESS_TOKEN     Required. Your Buffer API access token.
    BUFFER_ORG_ID           Required. Your Buffer organization ID (see buffer_get_account).
    BUFFER_CHANNEL_TWITTER  Optional. Your Twitter/X channel ID shorthand target.
    BUFFER_CHANNEL_LINKEDIN Optional. Your LinkedIn channel ID shorthand target.
    BUFFER_TWITTER_USERNAME Optional. Your Twitter username for reply-link lookups.
    CLOUDFLARE_ACCOUNT_ID       Optional. For buffer_upload_image via Cloudflare R2.
    CLOUDFLARE_R2_API_TOKEN     Optional. R2 API token.
    CLOUDFLARE_R2_BUCKET_NAME   Optional. R2 bucket name (default: "buffer-media").
    CLOUDFLARE_R2_PUBLIC_BASE_URL Optional. Public CDN base URL for uploaded files.
    TWITTER_BEARER_TOKEN    Optional. For buffer_get_reply_links (Twitter API v2).
"""

import asyncio
import json
import os
import re
import sys
from typing import Optional, List, Dict, Any
from enum import Enum
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field, field_validator, ConfigDict
from mcp.server.fastmcp import FastMCP

# Initialize the MCP server
mcp = FastMCP("buffer_mcp")

# Constants
GRAPHQL_URL = "https://api.buffer.com"
ACCESS_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN", "")

# Organization ID — find yours by calling buffer_get_account.
# NOTE: Buffer has separate "account ID" and "organization ID" values.
# Using the account ID here causes silent "Organization not found" errors.
# Always use the org ID from the `organizations` field, not `account.id`.
ORG_ID = os.environ.get("BUFFER_ORG_ID", "")

# Optional channel shorthands — set these to use "twitter" / "linkedin"
# as shorthand instead of raw channel IDs in tool calls.
KNOWN_CHANNELS: Dict[str, str] = {}
_ch_twitter = os.environ.get("BUFFER_CHANNEL_TWITTER", "")
_ch_linkedin = os.environ.get("BUFFER_CHANNEL_LINKEDIN", "")
if _ch_twitter:
    KNOWN_CHANNELS["twitter"] = _ch_twitter
if _ch_linkedin:
    KNOWN_CHANNELS["linkedin"] = _ch_linkedin


# ---- Shared Utilities -------------------------------------------------------

async def _graphql_request(
    query: str,
    variables: Optional[Dict[str, Any]] = None,
    max_retries: int = 3,
) -> dict:
    """
    Execute a GraphQL request against Buffer's API with automatic retry
    on rate limits (429) and transient server errors (500, 502, 503).

    Args:
        query: GraphQL query or mutation string.
        variables: Optional variables dict for the query.
        max_retries: Max retry attempts for rate limits / transient errors.

    Returns:
        The full JSON response body.

    Raises:
        ValueError: If BUFFER_ACCESS_TOKEN is not set.
        httpx.HTTPStatusError: If the API returns a non-2xx status after retries.
    """
    token = ACCESS_TOKEN
    if not token:
        raise ValueError(
            "BUFFER_ACCESS_TOKEN environment variable is not set. "
            "Get your token from Buffer → Settings → API."
        )

    payload: Dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    GRAPHQL_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=30.0,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            last_error = e
            # Retry on rate limit or transient server errors
            if e.response.status_code in (429, 500, 502, 503) and attempt < max_retries:
                wait = 2 ** attempt  # 1s, 2s, 4s exponential backoff
                await asyncio.sleep(wait)
                continue
            raise
        except httpx.TimeoutException as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
                continue
            raise

    raise last_error  # Should not reach here, but safety net


def _handle_api_error(e: Exception) -> str:
    """Consistent error formatting across all tools."""
    if isinstance(e, ValueError):
        return f"Configuration Error: {str(e)}"
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        try:
            body = e.response.json()
            errors = body.get("errors", [])
            if errors:
                msg = errors[0].get("message", "")
                return f"Error ({status}): {msg}"
        except Exception:
            pass
        if status == 401:
            return "Error: Invalid or expired access token."
        if status == 403:
            return "Error: Permission denied. Check your Buffer plan and token scope."
        if status == 429:
            return "Error: Rate limit exceeded. Wait a moment before retrying."
        return f"Error: Buffer API returned status {status}."
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Buffer may be experiencing issues."
    return f"Error: {type(e).__name__}: {str(e)}"


def _check_graphql_errors(result: dict) -> Optional[str]:
    """
    Check a GraphQL response for errors. Returns error string or None.
    Handles both top-level errors and union-type error variants.
    """
    # Top-level GraphQL errors
    if "errors" in result:
        msgs = [e.get("message", "Unknown error") for e in result["errors"]]
        return f"GraphQL Error: {'; '.join(msgs)}"
    return None


def _extract_post_result(data: dict, mutation_name: str) -> dict:
    """
    Extract result from a Buffer GraphQL mutation response.
    Handles the PostActionPayload union type.
    """
    payload = data.get("data", {}).get(mutation_name, {})
    # Check for error union types
    typename = payload.get("__typename", "")
    if typename == "PostActionSuccess":
        return {"success": True, "post": payload.get("post", {})}
    if typename == "DeletePostSuccess":
        return {"success": True, "id": payload.get("id")}
    # Any error type
    if "message" in payload:
        return {"success": False, "error": payload["message"]}
    # Fallback: if post is present, it succeeded
    if "post" in payload:
        return {"success": True, "post": payload["post"]}
    return {"success": False, "error": f"Unexpected response: {json.dumps(payload)}"}


def _format_post_summary(post: dict) -> str:
    """Format a post dict into a readable summary."""
    lines = []
    post_id = post.get("id", "N/A")
    status = post.get("status", "unknown")
    due_at = post.get("dueAt", "")
    text = post.get("text", "(no text)")

    display_text = text[:200] + "..." if len(text) > 200 else text
    lines.append(f"**[{status.upper()}]** {display_text}")
    lines.append(f"- **ID**: `{post_id}`")
    if due_at:
        lines.append(f"- **Scheduled**: {due_at}")
    return "\n".join(lines)


def _resolve_channel_id(channel: str) -> str:
    """
    Resolve a channel identifier to a Buffer channel ID.
    Accepts raw IDs or shorthand names ('twitter', 'linkedin').
    """
    lower = channel.lower().strip()
    if lower in KNOWN_CHANNELS:
        return KNOWN_CHANNELS[lower]
    return channel


def _build_post_input(
    channel_id: str,
    text: str,
    mode: str,
    *,
    scheduled_at: Optional[str] = None,
    image_urls: Optional[List[str]] = None,
    video_urls: Optional[List[str]] = None,
    video_thumbnails: Optional[List[str]] = None,
    linkedin_title: Optional[str] = None,
    linkedin_description: Optional[str] = None,
    linkedin_thumbnail_url: Optional[str] = None,
    thread_replies: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Build the Buffer GraphQL `CreatePostInput` object shared by create, batch,
    and update flows.

    Consolidating this here ensures every mutation path attaches assets and
    metadata identically. Previously each tool reimplemented this, and
    buffer_update_post's copy silently dropped videos, Twitter threads, and
    LinkedIn metadata on edit.

    Buffer AssetInput schema (2026-05-26): `assets` is a LIST of objects, each
    with exactly ONE of {"image": {...}} or {"video": {...}}. The old plural
    assets["images"]/assets["videos"] shape is dead.
    """
    post_input: Dict[str, Any] = {
        "channelId": channel_id,
        "text": text,
        "mode": mode,
        "schedulingType": "automatic",
    }
    if scheduled_at:
        post_input["dueAt"] = scheduled_at

    assets_list: List[Dict[str, Any]] = []
    if image_urls:
        for url in image_urls:
            assets_list.append({"image": {"url": url}})
    if video_urls:
        thumbs = video_thumbnails or []
        for i, url in enumerate(video_urls):
            entry: Dict[str, Any] = {"url": url}
            if i < len(thumbs) and thumbs[i]:
                entry["thumbnailUrl"] = thumbs[i]
            assets_list.append({"video": entry})
    if assets_list:
        post_input["assets"] = assets_list

    linkedin_meta: Dict[str, Any] = {}
    if linkedin_title:
        linkedin_meta["title"] = linkedin_title
    if linkedin_description:
        linkedin_meta["description"] = linkedin_description
    if linkedin_thumbnail_url:
        linkedin_meta["thumbnailUrl"] = linkedin_thumbnail_url

    twitter_meta: Dict[str, Any] = {}
    if thread_replies:
        # ThreadedPostInput.assets is NON_NULL — pass [] when a reply has no media.
        twitter_meta["thread"] = [{"text": t, "assets": []} for t in thread_replies]

    metadata_obj: Dict[str, Any] = {}
    if linkedin_meta:
        metadata_obj["linkedin"] = linkedin_meta
    if twitter_meta:
        metadata_obj["twitter"] = twitter_meta
    if metadata_obj:
        post_input["metadata"] = metadata_obj

    return post_input


# ---- ID + Schedule Helpers (canonical-id + pagination fixes) ----------------
#
# Bug 1 (Buffer ID rotation): buffer_create_post can return a transient
#   ObjectID that does NOT match the ID returned by a subsequent
#   buffer_list_posts for the same post. delete-by-create-time-ID returns
#   "Document not found"; delete-by-list-ID succeeds. The list-query ID is
#   treated as canonical because it is what Buffer's delete and update
#   mutations match against. Buffer appears to re-issue IDs across internal
#   stage transitions.
#
# Bug 2 (pagination): buffer_list_posts with limit=50 can return only 1 post
#   per channel when multiple are scheduled. Root cause: the `posts` query
#   had no `first` pagination arg and pageInfo cursors were not followed,
#   so Buffer's default page size (1) was used. Fixed via Relay-style
#   cursor pagination with explicit `first`/`after` args.

# Tolerance window when matching a Buffer post by `dueAt` (seconds).
_DUE_AT_TOLERANCE_S = 90
# How many leading characters of post text to compare when narrowing
# matches by content prefix.
_TEXT_MATCH_CHARS = 80


def _canonical_post_id(post: Dict[str, Any]) -> str:
    """
    Extract the canonical Buffer post ID from a post node.

    The `id` field in a list-query response is the stable, deletable ID.
    The `id` field in a createPost response can be a transient ObjectID
    that Buffer re-issues once the post lands in the scheduled queue.
    Always prefer the list-query value when both are available (Bug 1).
    """
    return str(post.get("id", "")) if post.get("id") is not None else ""


def _parse_iso(ts: str):
    """
    Parse an ISO 8601 string (with `Z` or `+00:00` offset) into a datetime.
    Returns None on any parse failure.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None


def _due_ats_match(a_iso: str, b_iso: str, tolerance_s: int = _DUE_AT_TOLERANCE_S) -> bool:
    """True if two ISO 8601 timestamps fall within tolerance_s of each other."""
    a = _parse_iso(a_iso)
    b = _parse_iso(b_iso)
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= tolerance_s


# Two GraphQL query shapes for `posts`. Buffer accepts `first`/`after`
# pagination inside the input object; if a future schema change rejects
# it, the legacy shape (no pagination args) is used as fallback so the
# tool degrades to the historical "returns whatever Buffer's default
# page is" behavior rather than failing outright.
_POSTS_QUERY_PAGINATED = """
query ListPosts($input: PostsInput!) {
    posts(input: $input) {
        edges {
            cursor
            node { id status dueAt text }
        }
        pageInfo { hasNextPage endCursor }
    }
}
"""

_POSTS_QUERY_LEGACY = """
query ListPosts($input: PostsInput!) {
    posts(input: $input) {
        edges {
            node { id status dueAt text }
        }
    }
}
"""


def _is_pagination_schema_error(err_text: str) -> bool:
    """Heuristic: is this GraphQL error caused by `first`/`after` not existing on PostsInput?"""
    if not err_text:
        return False
    lower = err_text.lower()
    markers = (
        "field \"first\"",
        "field 'first'",
        "field \"after\"",
        "field 'after'",
        "argument \"first\"",
        "argument 'first'",
        "is not defined",
        "unknown argument",
        "unknown field",
    )
    return any(m in lower for m in markers)


async def _list_posts_paginated(
    channel_id: Optional[str],
    target_limit: int,
    status: str = "scheduled",
) -> List[Dict[str, Any]]:
    """
    Fetch up to `target_limit` posts via Relay-style cursor pagination.

    Tries `first`/`after` pagination first. If Buffer's schema rejects
    those args, falls back to the legacy single-call shape (matching the
    pre-fix behavior). The legacy fallback is the safety net, NOT the
    happy path.
    """
    target_limit = max(1, min(int(target_limit), 200))
    page_size = min(target_limit, 50)

    base_filter: Dict[str, Any] = {"status": status}
    if channel_id:
        base_filter["channelIds"] = [channel_id]

    nodes: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    legacy_mode = False

    while len(nodes) < target_limit:
        input_obj: Dict[str, Any] = {
            "organizationId": ORG_ID,
            "filter": base_filter,
        }
        if not legacy_mode:
            input_obj["first"] = page_size
            if cursor:
                input_obj["after"] = cursor

        query = _POSTS_QUERY_LEGACY if legacy_mode else _POSTS_QUERY_PAGINATED
        try:
            result = await _graphql_request(query, {"input": input_obj})
        except httpx.HTTPStatusError as e:
            # Buffer returns 400 when `first`/`after` aren't in the schema.
            # Detect this and fall back to legacy mode automatically.
            if e.response.status_code == 400 and not legacy_mode:
                try:
                    body = e.response.json()
                    err_msgs = "; ".join(
                        x.get("message", "") for x in body.get("errors", [])
                    )
                except Exception:
                    err_msgs = str(e)
                if _is_pagination_schema_error(err_msgs):
                    legacy_mode = True
                    cursor = None
                    continue
            raise

        # Detect pagination schema mismatch and retry once in legacy mode.
        if "errors" in result and not legacy_mode:
            err_msgs = "; ".join(e.get("message", "") for e in result["errors"])
            if _is_pagination_schema_error(err_msgs):
                legacy_mode = True
                cursor = None
                continue
            # Real error, propagate.
            raise RuntimeError(f"GraphQL Error: {err_msgs}")

        if "errors" in result:
            err_msgs = "; ".join(e.get("message", "") for e in result["errors"])
            raise RuntimeError(f"GraphQL Error: {err_msgs}")

        posts_payload = result.get("data", {}).get("posts", {}) or {}
        edges = posts_payload.get("edges", []) or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if not node:
                continue
            nodes.append(node)
            if len(nodes) >= target_limit:
                break

        if legacy_mode:
            # Legacy shape has no cursor info, single page is all we get.
            break

        page_info = posts_payload.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return nodes[:target_limit]


async def _find_post_by_schedule(
    channel_id: str,
    scheduled_at_iso: str,
    text_prefix: Optional[str] = None,
    search_limit: int = 50,
) -> Optional[Dict[str, Any]]:
    """
    Re-list posts on `channel_id` and return the one whose dueAt matches
    `scheduled_at_iso` (within tolerance) and whose text starts with the
    given prefix (if provided). Returns the post dict (with canonical
    id) or None.

    This is the verify-ID-before-delete primitive. Use this any time you
    need the canonical ID for a post you scheduled earlier in the session.
    """
    if not channel_id or not scheduled_at_iso:
        return None

    try:
        nodes = await _list_posts_paginated(channel_id, target_limit=search_limit)
    except Exception:
        return None

    needle = (text_prefix or "")[:_TEXT_MATCH_CHARS].strip()

    for node in nodes:
        if not _due_ats_match(node.get("dueAt", ""), scheduled_at_iso):
            continue
        if needle:
            node_text = (node.get("text") or "")[:_TEXT_MATCH_CHARS].strip()
            if node_text != needle:
                continue
        return node

    return None


# ---- Platform-ID Extraction (engagement loop, 2026-04-27) -------------------
# Buffer's free-tier GraphQL exposes externalLink (the platform URL) but no
# native engagement metrics. We extract the platform-native ID from the URL
# so the engagement-pull pipeline can hit each platform's API directly.

# Twitter / X: https://x.com/{user_id_or_handle}/status/{tweet_id}
_TWITTER_ID_RE = re.compile(r"/status/(\d+)")
# LinkedIn: urn:li:share:{id} or urn:li:activity:{id} or urn:li:ugcPost:{id}
_LINKEDIN_URN_RE = re.compile(r"urn:li:(?:share|activity|ugcPost):(\d+)")


def _extract_platform_id(
    external_link: Optional[str],
    channel_service: Optional[str],
) -> Optional[str]:
    """
    Extract the platform-native ID from a Buffer post's externalLink.

    Returns the tweet_id for Twitter/X posts, the LinkedIn share/activity id
    for LinkedIn posts, or None when extraction fails or the platform is not
    yet supported. Engagement-pull scripts use this id to call the platform's
    own analytics endpoint (Twitter API v2, LinkedIn Marketing API, etc.).

    Examples:
        >>> _extract_platform_id("https://x.com/1847432077639114752/status/2048847401138483469", "twitter")
        "2048847401138483469"
        >>> _extract_platform_id("https://www.linkedin.com/feed/update/urn:li:share:7453592437333078016", "linkedin")
        "7453592437333078016"
    """
    if not external_link:
        return None
    service = (channel_service or "").lower()
    if service in ("twitter", "x"):
        m = _TWITTER_ID_RE.search(external_link)
        return m.group(1) if m else None
    if service == "linkedin":
        m = _LINKEDIN_URN_RE.search(external_link)
        return m.group(1) if m else None
    return None


# ---- Input Models ------------------------------------------------------------

class CreatePostInput(BaseModel):
    """Input model for creating a Buffer post via GraphQL."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel_id: str = Field(
        ...,
        description=(
            "Buffer channel ID to post to. Use buffer_list_channels to get IDs, "
            "or use shorthand: 'twitter' or 'linkedin'."
        ),
        min_length=1,
    )
    text: str = Field(
        ...,
        description="The post content text.",
        min_length=1,
        max_length=5000,
    )
    scheduled_at: Optional[str] = Field(
        default=None,
        description=(
            "When to publish, in ISO 8601 UTC format (e.g., '2026-03-28T16:00:00Z'). "
            "If omitted, adds to queue."
        ),
    )
    image_urls: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of publicly accessible image URLs to attach. "
            "Buffer requires images to be hosted at public URLs."
        ),
    )
    video_urls: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of publicly accessible mp4 URLs to attach. Maps to Buffer's "
            "`assets.videos` via VideoAssetInput. Buffer will fetch each URL "
            "and re-host the video. Mutually exclusive with image_urls per "
            "channel (Twitter/LinkedIn only allow one media type per post). "
            "Hosting note: Buffer's fetcher rejects some CDNs (standard "
            "catbox.moe returns 0-byte on cross-origin); litter.catbox.moe, "
            "Cloudinary, and S3 work. Pass a `video_thumbnails` list with "
            "the same length to pre-supply poster frames."
        ),
    )
    video_thumbnails: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional list of thumbnail URLs, same length as video_urls, "
            "used as VideoAssetInput.thumbnailUrl. If omitted, Buffer "
            "auto-extracts a poster frame."
        ),
    )
    share_now: Optional[bool] = Field(
        default=False,
        description="If true, post immediately instead of scheduling."
    )
    share_next: Optional[bool] = Field(
        default=False,
        description=(
            "If true, move this post to the front of the queue (next available "
            "slot) instead of appending to the end. Use for timely or reactive "
            "content that must go out soon without blasting it immediately. "
            "Takes precedence over addToQueue but not over share_now or scheduled_at."
        ),
    )
    linkedin_title: Optional[str] = Field(
        default=None,
        description="LinkedIn article title (only for LinkedIn channel posts with a link)."
    )
    linkedin_description: Optional[str] = Field(
        default=None,
        description="LinkedIn article description / subtitle."
    )
    linkedin_thumbnail_url: Optional[str] = Field(
        default=None,
        description="Custom thumbnail URL for LinkedIn link preview."
    )
    thread_replies: Optional[List[str]] = Field(
        default=None,
        description=(
            "List of reply tweet text bodies to schedule as a Twitter thread. "
            "When supplied, this post is published as the parent tweet and each "
            "string in the list becomes a sequential reply tweet. Maps to "
            "TwitterPostMetadataInput.thread via [{text}, ...]. Tip: the parent "
            "tweet should NOT contain a URL (Twitter's algorithm suppresses "
            "posts with links in the body); put your CTA link in the first reply."
        ),
    )

    @field_validator("channel_id")
    @classmethod
    def resolve_channel(cls, v: str) -> str:
        return _resolve_channel_id(v)


class DeletePostInput(BaseModel):
    """Input model for deleting a Buffer post."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    post_id: str = Field(
        ...,
        description="The Buffer post ID to delete (get from buffer_list_posts).",
        min_length=1,
    )
    expected_scheduled_at: Optional[str] = Field(
        default=None,
        description=(
            "Optional ISO 8601 UTC timestamp the target post should be "
            "scheduled at. When provided alongside `verify_channel_id`, the "
            "tool re-lists posts on that channel and uses the canonical ID "
            "matching this dueAt instead of trusting `post_id` blindly. "
            "Recommended workaround for Buffer's ID rotation bug: the ID "
            "returned by createPost can differ from the list-query ID."
        ),
    )
    verify_channel_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional channel ID or shorthand ('twitter', 'linkedin') used "
            "alongside `expected_scheduled_at` to look up the canonical ID."
        ),
    )
    verify_text_prefix: Optional[str] = Field(
        default=None,
        description=(
            "Optional first ~80 chars of the post text, used to disambiguate "
            "when multiple posts share a dueAt."
        ),
    )

    @field_validator("verify_channel_id")
    @classmethod
    def resolve_verify_channel(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return _resolve_channel_id(v)
        return v


class ListPostsInput(BaseModel):
    """Input model for listing scheduled posts."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel_id: Optional[str] = Field(
        default=None,
        description=(
            "Filter to a specific channel. Use shorthand ('twitter', 'linkedin') "
            "or a raw channel ID. If omitted, lists posts across all channels."
        ),
    )
    limit: Optional[int] = Field(
        default=20,
        description="Maximum number of posts to return (1 to 50).",
        ge=1, le=50,
    )

    @field_validator("channel_id")
    @classmethod
    def resolve_channel(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return _resolve_channel_id(v)
        return v


class GetPostInput(BaseModel):
    """Input model for getting a single post's details."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    post_id: str = Field(
        ...,
        description="The Buffer post ID.",
        min_length=1,
    )


# ---- Tools: Channels --------------------------------------------------------

@mcp.tool(
    name="buffer_list_channels",
    annotations={
        "title": "List Buffer Channels",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_list_channels() -> str:
    """
    List all social media channels (profiles) connected to your Buffer account.

    Returns each channel's ID, name, service (Twitter/LinkedIn/etc.).
    Use channel IDs with buffer_create_post and buffer_list_posts.

    Returns:
        str: Channel list with IDs, names, and services.

    Examples:
        - Use when: "What channels are connected to Buffer?"
        - Use when: "Get my Buffer channel IDs for scheduling"
    """
    try:
        query = """
        query {
            account {
                id
                name
                channels {
                    id
                    name
                    service
                }
            }
        }
        """
        result = await _graphql_request(query)

        err = _check_graphql_errors(result)
        if err:
            return err

        account = result.get("data", {}).get("account", {})
        channels = account.get("channels", [])

        if not channels:
            return "No channels found. Connect social accounts in Buffer settings."

        lines = [f"# Buffer Channels (Account: {account.get('name', 'N/A')})", ""]
        for ch in channels:
            service = ch.get("service", "unknown")
            name = ch.get("name", "unnamed")
            ch_id = ch.get("id", "N/A")
            lines.append(f"- **{service}** / {name}: `{ch_id}`")

        lines.append("")
        lines.append(f"**Total**: {len(channels)} channels")
        return "\n".join(lines)

    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: Create Post -----------------------------------------------------

@mcp.tool(
    name="buffer_create_post",
    annotations={
        "title": "Create and Schedule Buffer Post",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def buffer_create_post(params: CreatePostInput) -> str:
    """
    Create and schedule a social media post through Buffer.

    Supports custom scheduling (specific datetime), immediate posting,
    or queue mode. Can attach images via public URLs.

    Args:
        params (CreatePostInput): Validated input containing:
            - channel_id (str): Channel ID or shorthand ('twitter', 'linkedin') (required)
            - text (str): Post content (required)
            - scheduled_at (Optional[str]): ISO 8601 UTC datetime for custom scheduling
            - image_urls (Optional[List[str]]): Public image URLs to attach
            - share_now (Optional[bool]): Post immediately (default: false)

    Returns:
        str: Confirmation with post ID, status, and scheduled time.

    Examples:
        - Use when: "Schedule this tweet for tomorrow at 2pm UTC"
        - Use when: "Post this to LinkedIn right now"
        - Use when: "Add this to my Twitter queue with an image"
    """
    try:
        # Determine scheduling mode
        if params.share_now:
            mode = "shareNow"
        elif params.scheduled_at:
            mode = "customScheduled"
        elif params.share_next:
            mode = "shareNext"
        else:
            mode = "addToQueue"

        # Build the mutation input (assets + LinkedIn/Twitter metadata) via the
        # shared builder so create/batch/update stay in lock-step.
        post_input = _build_post_input(
            channel_id=params.channel_id,
            text=params.text,
            mode=mode,
            scheduled_at=params.scheduled_at,
            image_urls=params.image_urls,
            video_urls=params.video_urls,
            video_thumbnails=params.video_thumbnails,
            linkedin_title=params.linkedin_title,
            linkedin_description=params.linkedin_description,
            linkedin_thumbnail_url=params.linkedin_thumbnail_url,
            thread_replies=params.thread_replies,
        )

        mutation = """
        mutation CreatePost($input: CreatePostInput!) {
            createPost(input: $input) {
                ... on PostActionSuccess {
                    __typename
                    post {
                        id
                        status
                        dueAt
                        text
                    }
                }
                ... on NotFoundError { __typename message }
                ... on UnauthorizedError { __typename message }
                ... on UnexpectedError { __typename message }
                ... on InvalidInputError { __typename message }
                ... on LimitReachedError { __typename message }
                ... on RestProxyError { __typename message }
            }
        }
        """

        result = await _graphql_request(mutation, {"input": post_input})

        err = _check_graphql_errors(result)
        if err:
            return err

        extracted = _extract_post_result(result, "createPost")

        if not extracted["success"]:
            return f"Error creating post: {extracted.get('error', 'Unknown error')}"

        post = extracted["post"]
        create_time_id = _canonical_post_id(post)
        canonical_id = create_time_id
        canonical_due_at = post.get("dueAt", "") or params.scheduled_at or ""

        # Verify the canonical (delete-able) ID by re-listing. This guards
        # against Bug 1 (ID rotation): Buffer can return a transient
        # ObjectID from createPost that does not match the ID surfaced by
        # subsequent posts() queries, breaking later delete/update calls.
        if params.scheduled_at and not params.share_now:
            verified = await _find_post_by_schedule(
                channel_id=params.channel_id,
                scheduled_at_iso=params.scheduled_at,
                text_prefix=params.text,
            )
            if verified and _canonical_post_id(verified):
                canonical_id = _canonical_post_id(verified)
                canonical_due_at = verified.get("dueAt") or canonical_due_at

        lines = [
            "# Post Created",
            "",
            f"- **ID**: `{canonical_id or 'N/A'}`",
            f"- **Status**: {post.get('status', 'N/A')}",
            f"- **Scheduled**: {canonical_due_at or 'In queue'}",
            f"- **Channel**: `{params.channel_id}`",
        ]

        if canonical_id and create_time_id and canonical_id != create_time_id:
            lines.append(
                f"- **Canonical ID source**: list-query (create-time ID was "
                f"`{create_time_id}`, replaced for delete/update safety)"
            )

        if params.image_urls:
            lines.append(f"- **Images**: {len(params.image_urls)} attached")

        text_preview = params.text[:100] + "..." if len(params.text) > 100 else params.text
        lines.append(f"- **Text**: {text_preview}")

        return "\n".join(lines)

    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: Delete Post -----------------------------------------------------

@mcp.tool(
    name="buffer_delete_post",
    annotations={
        "title": "Delete Buffer Post",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_delete_post(params: DeletePostInput) -> str:
    """
    Permanently delete a post from Buffer. This cannot be undone.

    Works on scheduled, queued, or draft posts.

    Args:
        params (DeletePostInput): Validated input containing:
            - post_id (str): ID of the post to delete (required)

    Returns:
        str: Confirmation that the post was deleted.

    Examples:
        - Use when: "Delete that test post from Buffer"
        - Use when: "Remove post 69c59c2220f76613b72d89ea from the queue"
    """
    try:
        target_id = params.post_id
        verify_note = ""

        # If the caller supplied schedule + channel, refresh the ID to the
        # canonical (delete-able) one before issuing the mutation. Guards
        # against Bug 1 (post-ID rotation between create and list).
        if params.expected_scheduled_at and params.verify_channel_id:
            verified = await _find_post_by_schedule(
                channel_id=params.verify_channel_id,
                scheduled_at_iso=params.expected_scheduled_at,
                text_prefix=params.verify_text_prefix,
            )
            if verified is None:
                return (
                    f"No scheduled post found at `{params.expected_scheduled_at}` "
                    f"on channel `{params.verify_channel_id}`. Nothing to delete."
                )
            verified_id = _canonical_post_id(verified)
            if verified_id and verified_id != target_id:
                verify_note = (
                    f"\n(verify-by-schedule: provided `{params.post_id}`, "
                    f"using canonical `{verified_id}`)"
                )
            target_id = verified_id or target_id

        mutation = """
        mutation DeletePost($input: DeletePostInput!) {
            deletePost(input: $input) {
                ... on DeletePostSuccess {
                    __typename
                    id
                }
                ... on VoidMutationError {
                    __typename
                    message
                }
            }
        }
        """

        result = await _graphql_request(mutation, {"input": {"id": target_id}})

        err = _check_graphql_errors(result)
        if err:
            return err

        payload = result.get("data", {}).get("deletePost", {})
        typename = payload.get("__typename", "")

        if typename == "DeletePostSuccess":
            return f"Post `{payload.get('id', target_id)}` deleted successfully.{verify_note}"
        if "message" in payload:
            return f"Error deleting post: {payload['message']}{verify_note}"

        return f"Post `{target_id}` delete request sent.{verify_note}"

    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: Find Post By Schedule -------------------------------------------

class FindPostByScheduleInput(BaseModel):
    """Input model for finding a scheduled post by channel + dueAt + text prefix."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel_id: str = Field(
        ...,
        description="Buffer channel ID or shorthand ('twitter', 'linkedin').",
        min_length=1,
    )
    scheduled_at: str = Field(
        ...,
        description="ISO 8601 UTC timestamp of the scheduled post to find.",
        min_length=1,
    )
    text_prefix: Optional[str] = Field(
        default=None,
        description=(
            "Optional first ~80 chars of the post text to disambiguate "
            "when multiple posts share a dueAt slot."
        ),
    )

    @field_validator("channel_id")
    @classmethod
    def resolve_channel(cls, v: str) -> str:
        return _resolve_channel_id(v)


@mcp.tool(
    name="buffer_find_post_by_schedule",
    annotations={
        "title": "Find Buffer Post by Schedule",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_find_post_by_schedule(params: FindPostByScheduleInput) -> str:
    """
    Look up the canonical Buffer post ID for a known channel + dueAt slot.

    Use this any time you scheduled a post earlier and now need to delete or
    update it. Buffer can return rotating IDs from createPost (ID rotation
    bug), so a freshly re-listed match is the only safe source for
    delete/update operations.

    Returns the post's canonical ID, status, dueAt, and text preview, or a
    "not found" message if no scheduled post matches.

    Examples:
        - Use when: "I scheduled a tweet for 7:30 PM UTC, give me its current ID"
        - Use when: about to delete a post but the saved ID is stale
    """
    try:
        node = await _find_post_by_schedule(
            channel_id=params.channel_id,
            scheduled_at_iso=params.scheduled_at,
            text_prefix=params.text_prefix,
        )
        if not node:
            return (
                f"No scheduled post found at `{params.scheduled_at}` on channel "
                f"`{params.channel_id}`."
            )

        canonical = _canonical_post_id(node)
        lines = [
            "# Canonical Post Match",
            "",
            f"- **ID**: `{canonical}`",
            f"- **Status**: {node.get('status', 'N/A')}",
            f"- **Scheduled**: {node.get('dueAt', 'N/A')}",
        ]
        text = node.get("text", "") or ""
        text_preview = text[:120] + "..." if len(text) > 120 else text
        lines.append(f"- **Text**: {text_preview}")
        return "\n".join(lines)
    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: List Posts -------------------------------------------------------

@mcp.tool(
    name="buffer_list_posts",
    annotations={
        "title": "List Scheduled Buffer Posts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_list_posts(params: ListPostsInput) -> str:
    """
    List scheduled posts in your Buffer queue.

    Can filter by channel or show all scheduled posts. Returns post IDs,
    text previews, and scheduled times.

    Args:
        params (ListPostsInput): Validated input containing:
            - channel_id (Optional[str]): Filter to channel (shorthand or ID)
            - limit (Optional[int]): Max results, 1-50 (default: 20)

    Returns:
        str: List of scheduled posts with IDs and schedule times.

    Examples:
        - Use when: "Show me what's scheduled on Twitter"
        - Use when: "What's in my Buffer queue?"
        - Use when: "List all upcoming posts"
    """
    try:
        try:
            nodes = await _list_posts_paginated(
                channel_id=params.channel_id,
                target_limit=params.limit,
            )
        except RuntimeError as re:
            err_str = str(re)
            # Known issue: org-level reads may return FORBIDDEN.
            if "FORBIDDEN" in err_str.upper() or "can not access" in err_str.lower():
                return (
                    "Error: The Buffer API token does not have organization-level read access. "
                    "Posts can still be created and deleted. To see your queue, check Buffer's web UI."
                )
            return err_str

        if not nodes:
            channel_note = f" for channel `{params.channel_id}`" if params.channel_id else ""
            return f"No scheduled posts found{channel_note}."

        lines = ["# Scheduled Posts", ""]
        for i, node in enumerate(nodes, 1):
            lines.append(f"## {i}.")
            lines.append(_format_post_summary(node))
            lines.append("")

        lines.append(f"**Showing**: {len(nodes)} posts")
        return "\n".join(lines)

    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: Batch Create ----------------------------------------------------

class BatchCreatePostInput(BaseModel):
    """Input model for creating multiple posts in one call."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    channel_id: str = Field(
        ...,
        description="Buffer channel ID or shorthand ('twitter', 'linkedin').",
        min_length=1,
    )
    posts: List[Dict[str, Any]] = Field(
        ...,
        description=(
            "List of post objects. Each must have 'text' (str) and optionally "
            "'scheduled_at' (ISO 8601 UTC str), 'image_urls' (list of str), "
            "'linkedin_title' (str), 'linkedin_description' (str), "
            "and 'linkedin_thumbnail_url' (str)."
        ),
        min_length=1,
        max_length=25,
    )

    @field_validator("channel_id")
    @classmethod
    def resolve_channel(cls, v: str) -> str:
        return _resolve_channel_id(v)


@mcp.tool(
    name="buffer_batch_create_posts",
    annotations={
        "title": "Batch Create Buffer Posts",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def buffer_batch_create_posts(params: BatchCreatePostInput) -> str:
    """
    Create multiple scheduled posts in a single call.

    Schedules each post individually through Buffer's createPost mutation.
    Useful for scheduling a full day or week of content at once.

    Args:
        params (BatchCreatePostInput): Validated input containing:
            - channel_id (str): Channel ID or shorthand (required)
            - posts (List[dict]): List of post objects, each with:
                - text (str): Post content (required)
                - scheduled_at (str): ISO 8601 UTC datetime (optional)
                - image_urls (List[str]): Public image URLs (optional)

    Returns:
        str: Summary of all created posts with IDs and statuses.

    Examples:
        - Use when: "Schedule these 6 posts for Friday and Saturday"
        - Use when: "Batch schedule all the content for this week"
    """
    results = []
    errors = []

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
        createPost(input: $input) {
            ... on PostActionSuccess {
                __typename
                post { id status dueAt text }
            }
            ... on NotFoundError { __typename message }
            ... on UnauthorizedError { __typename message }
            ... on UnexpectedError { __typename message }
            ... on InvalidInputError { __typename message }
            ... on LimitReachedError { __typename message }
            ... on RestProxyError { __typename message }
        }
    }
    """

    for i, post_data in enumerate(params.posts, 1):
        try:
            text = post_data.get("text", "")
            scheduled_at = post_data.get("scheduled_at")
            image_urls = post_data.get("image_urls", [])

            if not text:
                errors.append(f"Post {i}: Missing 'text' field.")
                continue

            share_next = post_data.get("share_next", False)
            if scheduled_at:
                batch_mode = "customScheduled"
            elif share_next:
                batch_mode = "shareNext"
            else:
                batch_mode = "addToQueue"

            post_input = _build_post_input(
                channel_id=params.channel_id,
                text=text,
                mode=batch_mode,
                scheduled_at=scheduled_at,
                image_urls=image_urls,
                video_urls=post_data.get("video_urls"),
                video_thumbnails=post_data.get("video_thumbnails"),
                linkedin_title=post_data.get("linkedin_title"),
                linkedin_description=post_data.get("linkedin_description"),
                linkedin_thumbnail_url=post_data.get("linkedin_thumbnail_url"),
                thread_replies=post_data.get("thread_replies"),
            )

            result = await _graphql_request(mutation, {"input": post_input})

            err = _check_graphql_errors(result)
            if err:
                errors.append(f"Post {i}: {err}")
                continue

            extracted = _extract_post_result(result, "createPost")

            if extracted["success"]:
                post = extracted["post"]
                results.append({
                    "index": i,
                    "id": post.get("id"),
                    "status": post.get("status"),
                    "dueAt": post.get("dueAt"),
                    "text_preview": text[:80],
                })
            else:
                errors.append(f"Post {i}: {extracted.get('error', 'Unknown error')}")

        except Exception as e:
            errors.append(f"Post {i}: {_handle_api_error(e)}")

    # Format output
    lines = ["# Batch Post Results", ""]

    if results:
        lines.append(f"**Created**: {len(results)} / {len(params.posts)} posts")
        lines.append("")
        for r in results:
            preview = r["text_preview"] + "..." if len(r["text_preview"]) >= 80 else r["text_preview"]
            lines.append(f"- Post {r['index']}: `{r['id']}` | {r['status']} | {r.get('dueAt', 'queued')}")
            lines.append(f"  {preview}")

    if errors:
        lines.append("")
        lines.append("**Errors:**")
        for err in errors:
            lines.append(f"- {err}")

    return "\n".join(lines)


# ---- Tools: Account Info ----------------------------------------------------

@mcp.tool(
    name="buffer_get_account",
    annotations={
        "title": "Get Buffer Account Info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_get_account() -> str:
    """
    Get Buffer account information including organization ID and connected channels.

    Returns:
        str: Account details with org ID and channel summary.

    Examples:
        - Use when: "What Buffer account am I connected to?"
        - Use when: "Show my Buffer org ID"
    """
    try:
        query = """
        query {
            account {
                id
                name
                channels {
                    id
                    name
                    service
                }
            }
        }
        """
        result = await _graphql_request(query)

        err = _check_graphql_errors(result)
        if err:
            return err

        account = result.get("data", {}).get("account", {})
        channels = account.get("channels", [])

        lines = [
            "# Buffer Account",
            "",
            f"- **Account ID**: `{account.get('id', 'N/A')}`",
            f"- **Name**: {account.get('name', 'N/A')}",
            f"- **Channels**: {len(channels)} connected",
            "",
        ]

        if channels:
            lines.append("## Connected Channels")
            for ch in channels:
                lines.append(f"- {ch.get('service', '?')}: {ch.get('name', '?')} (`{ch.get('id', '?')}`)")

        return "\n".join(lines)

    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: Update Post (delete + recreate) ---------------------------------

class UpdatePostInput(BaseModel):
    """Input model for updating an existing Buffer post (delete + recreate)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    post_id: str = Field(
        ...,
        description="ID of the existing post to update.",
        min_length=1,
    )
    channel_id: str = Field(
        ...,
        description="Channel ID or shorthand ('twitter', 'linkedin'). Must match the original post's channel.",
        min_length=1,
    )
    text: str = Field(
        ...,
        description="The new post content text.",
        min_length=1,
        max_length=5000,
    )
    scheduled_at: Optional[str] = Field(
        default=None,
        description="New schedule time in ISO 8601 UTC. If omitted, adds to queue.",
    )
    image_urls: Optional[List[str]] = Field(
        default=None,
        description="New image URLs to attach (replaces any previous images).",
    )
    video_urls: Optional[List[str]] = Field(
        default=None,
        description="New mp4 URLs to attach (replaces any previous media).",
    )
    video_thumbnails: Optional[List[str]] = Field(
        default=None,
        description="Optional poster-frame URLs, same length as video_urls.",
    )
    linkedin_title: Optional[str] = Field(
        default=None, description="LinkedIn article title for the replacement post."
    )
    linkedin_description: Optional[str] = Field(
        default=None, description="LinkedIn article description for the replacement post."
    )
    linkedin_thumbnail_url: Optional[str] = Field(
        default=None, description="LinkedIn link-preview thumbnail for the replacement post."
    )
    thread_replies: Optional[List[str]] = Field(
        default=None,
        description="Twitter thread reply bodies for the replacement post.",
    )
    expected_scheduled_at: Optional[str] = Field(
        default=None,
        description=(
            "Optional ISO 8601 UTC timestamp the post being replaced is "
            "scheduled at. When provided, the tool re-lists posts on "
            "channel_id and resolves the canonical (delete-able) ID via "
            "dueAt match instead of trusting post_id blindly — the same "
            "guard buffer_delete_post uses against Buffer's ID-rotation bug. "
            "Strongly recommended: update is a delete+recreate, so a stale "
            "post_id means the delete no-ops and you end up with a duplicate."
        ),
    )
    verify_text_prefix: Optional[str] = Field(
        default=None,
        description=(
            "Optional first ~80 chars of the existing post's text, used with "
            "expected_scheduled_at to disambiguate posts sharing a dueAt."
        ),
    )

    @field_validator("channel_id")
    @classmethod
    def resolve_channel(cls, v: str) -> str:
        return _resolve_channel_id(v)


@mcp.tool(
    name="buffer_update_post",
    annotations={
        "title": "Update Buffer Post",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def buffer_update_post(params: UpdatePostInput) -> str:
    """
    Update an existing scheduled post by deleting it and creating a new one.

    Buffer's GraphQL API has no updatePost mutation, so this tool performs
    an atomic delete-then-create. If the delete succeeds but create fails,
    the original post is gone. Use with care.

    Args:
        params (UpdatePostInput): Validated input containing:
            - post_id (str): ID of the post to replace (required)
            - channel_id (str): Channel ID or shorthand (required)
            - text (str): New post content (required)
            - scheduled_at (Optional[str]): New schedule time (ISO 8601 UTC)
            - image_urls (Optional[List[str]]): New image URLs

    Returns:
        str: Confirmation with old post deleted and new post details.

    Examples:
        - Use when: "Fix the typo in that scheduled tweet"
        - Use when: "Reschedule post X to 3pm instead of 2pm"
    """
    try:
        # Step 0: Resolve the canonical (delete-able) ID before destroying
        # anything. buffer_update_post is a delete+recreate; if post_id is
        # stale (Buffer's ID-rotation bug) the delete silently no-ops and the
        # recreate leaves a DUPLICATE. Re-list by schedule to get the real ID,
        # mirroring buffer_delete_post's guard.
        target_id = params.post_id
        verify_note = ""
        if params.expected_scheduled_at:
            verified = await _find_post_by_schedule(
                channel_id=params.channel_id,
                scheduled_at_iso=params.expected_scheduled_at,
                text_prefix=params.verify_text_prefix,
            )
            if verified is None:
                return (
                    f"No scheduled post found at `{params.expected_scheduled_at}` "
                    f"on channel `{params.channel_id}`. Aborting update so an "
                    f"orphan duplicate is not created."
                )
            verified_id = _canonical_post_id(verified)
            if verified_id and verified_id != target_id:
                verify_note = (
                    f"\n(verify-by-schedule: provided `{params.post_id}`, "
                    f"deleted canonical `{verified_id}`)"
                )
            target_id = verified_id or target_id

        # Step 1: Delete the old post
        delete_mutation = """
        mutation DeletePost($input: DeletePostInput!) {
            deletePost(input: $input) {
                ... on DeletePostSuccess { __typename id }
                ... on VoidMutationError { __typename message }
            }
        }
        """
        del_result = await _graphql_request(delete_mutation, {"input": {"id": target_id}})

        err = _check_graphql_errors(del_result)
        if err:
            return f"Error deleting old post: {err}"

        del_payload = del_result.get("data", {}).get("deletePost", {})
        if del_payload.get("__typename") == "VoidMutationError":
            return f"Error deleting old post: {del_payload.get('message', 'Unknown error')}"

        # Step 2: Create the replacement. Uses the shared builder so media and
        # metadata (videos, Twitter threads, LinkedIn fields) survive an edit —
        # the old inline builder dropped everything but images.
        mode = "customScheduled" if params.scheduled_at else "addToQueue"
        post_input = _build_post_input(
            channel_id=params.channel_id,
            text=params.text,
            mode=mode,
            scheduled_at=params.scheduled_at,
            image_urls=params.image_urls,
            video_urls=params.video_urls,
            video_thumbnails=params.video_thumbnails,
            linkedin_title=params.linkedin_title,
            linkedin_description=params.linkedin_description,
            linkedin_thumbnail_url=params.linkedin_thumbnail_url,
            thread_replies=params.thread_replies,
        )

        create_mutation = """
        mutation CreatePost($input: CreatePostInput!) {
            createPost(input: $input) {
                ... on PostActionSuccess { __typename post { id status dueAt text } }
                ... on NotFoundError { __typename message }
                ... on UnauthorizedError { __typename message }
                ... on UnexpectedError { __typename message }
                ... on InvalidInputError { __typename message }
                ... on LimitReachedError { __typename message }
                ... on RestProxyError { __typename message }
            }
        }
        """
        create_result = await _graphql_request(create_mutation, {"input": post_input})

        err = _check_graphql_errors(create_result)
        if err:
            return f"Old post `{target_id}` was deleted, but new post failed: {err}"

        extracted = _extract_post_result(create_result, "createPost")
        if not extracted["success"]:
            return (
                f"Old post `{target_id}` was deleted, but new post failed: "
                f"{extracted.get('error', 'Unknown error')}"
            )

        post = extracted["post"]
        lines = [
            "# Post Updated",
            "",
            f"- **Old post deleted**: `{target_id}`",
            f"- **New post ID**: `{post.get('id', 'N/A')}`",
            f"- **Status**: {post.get('status', 'N/A')}",
            f"- **Scheduled**: {post.get('dueAt', 'In queue')}",
            f"- **Channel**: `{params.channel_id}`",
        ]
        text_preview = params.text[:100] + "..." if len(params.text) > 100 else params.text
        lines.append(f"- **Text**: {text_preview}")
        if verify_note:
            lines.append(verify_note.strip())
        return "\n".join(lines)

    except Exception as e:
        return _handle_api_error(e)


# ---- Tools: Health Check  ---------------------------------------------------

@mcp.tool(
    name="buffer_health_check",
    annotations={
        "title": "Buffer API Health Check",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_health_check() -> str:
    """
    Verify the Buffer API token is valid and the API is reachable.

    Makes a lightweight account query to test authentication and connectivity.
    Use this to diagnose issues before attempting to create or delete posts.

    Returns:
        str: Health status with token validity, account name, and channel count.

    Examples:
        - Use when: "Is the Buffer connection working?"
        - Use when: Posts are failing and you need to check if the token expired
    """
    try:
        token = ACCESS_TOKEN
        if not token:
            return (
                "FAIL: BUFFER_ACCESS_TOKEN environment variable is not set.\n"
                "Set it before running the server."
            )

        query = """
        query {
            account {
                id
                name
                channels { id service }
            }
        }
        """
        result = await _graphql_request(query)

        err = _check_graphql_errors(result)
        if err:
            return f"FAIL: Token is set but API returned error: {err}"

        account = result.get("data", {}).get("account", {})
        channels = account.get("channels", [])

        lines = [
            "# Buffer Health Check: PASS",
            "",
            f"- **Token**: Valid",
            f"- **Account**: {account.get('name', 'N/A')} (`{account.get('id', 'N/A')}`)",
            f"- **Channels**: {len(channels)} connected",
            f"- **API**: api.buffer.com responding normally",
        ]

        for ch in channels:
            lines.append(f"  - {ch.get('service', '?')}: `{ch.get('id', '?')}`")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return (
                "FAIL: Token is expired or invalid (HTTP 401).\n"
                "Regenerate your Buffer access token and update BUFFER_ACCESS_TOKEN."
            )
        return f"FAIL: API returned HTTP {e.response.status_code}."
    except httpx.TimeoutException:
        return "FAIL: API request timed out. Buffer may be down."
    except Exception as e:
        return f"FAIL: {type(e).__name__}: {str(e)}"


# ---- Tools: Upload Media (Cloudflare R2) ------------------------------------
#
# Buffer's media fetcher is picky about CDN sources. Many free hosts (catbox,
# imgur) return 0-byte or 429 responses to Buffer's node-fetch UA. Cloudflare R2
# has no UA restrictions, no rate limits, and files persist indefinitely.
#
# Required env vars for this tool:
#   CLOUDFLARE_ACCOUNT_ID        Your Cloudflare account ID
#   CLOUDFLARE_R2_API_TOKEN      R2 API token with Object:Write permission
#   CLOUDFLARE_R2_BUCKET_NAME    Bucket name (default: "buffer-media")
#   CLOUDFLARE_R2_PUBLIC_BASE_URL Public CDN URL for the bucket


async def _upload_to_r2(file_path: str) -> str:
    """
    Upload a local file to Cloudflare R2 and return its public CDN URL.

    Reads credentials from environment variables. Raises ValueError if any
    required variable is missing, RuntimeError on upload failure.
    """
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    api_token = os.environ.get("CLOUDFLARE_R2_API_TOKEN", "")
    bucket = os.environ.get("CLOUDFLARE_R2_BUCKET_NAME", "buffer-media")
    public_base = os.environ.get("CLOUDFLARE_R2_PUBLIC_BASE_URL", "").rstrip("/")

    missing = [k for k, v in {
        "CLOUDFLARE_ACCOUNT_ID": account_id,
        "CLOUDFLARE_R2_API_TOKEN": api_token,
        "CLOUDFLARE_R2_PUBLIC_BASE_URL": public_base,
    }.items() if not v]
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    from pathlib import Path
    import mimetypes

    path = Path(file_path)
    object_key = f"media/{path.name}"
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    upload_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/r2/buckets/{bucket}/objects/{object_key}"
    )

    with open(file_path, "rb") as f:
        data = f.read()

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.put(
            upload_url,
            content=data,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": content_type,
            },
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"R2 upload failed ({resp.status_code}): {resp.text[:300]}")

    return f"{public_base}/{object_key}"


@mcp.tool(
    name="buffer_upload_image",
    annotations={
        "title": "Upload Media for Buffer Post (Cloudflare R2)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def buffer_upload_image(file_path: str) -> str:
    """
    Upload a local image or video file to Cloudflare R2 and return a public
    URL suitable for Buffer's media fetcher.

    Buffer requires media to be served from public URLs. Many free hosts fail
    with Buffer's node-fetch UA (catbox, imgur return 0-byte or 429). R2 has
    no UA discrimination, no rate limits, and files persist indefinitely.

    Requires env vars:
        CLOUDFLARE_ACCOUNT_ID
        CLOUDFLARE_R2_API_TOKEN       (Object:Write permission)
        CLOUDFLARE_R2_BUCKET_NAME     (default: "buffer-media")
        CLOUDFLARE_R2_PUBLIC_BASE_URL (public CDN URL for your bucket)

    Supports images (PNG, JPG, GIF, WEBP) and videos (MP4, MOV, WEBM).
    The returned URL can be passed to buffer_create_post's image_urls or
    video_urls fields.

    Args:
        file_path (str): Absolute or relative path to the file on disk.

    Returns:
        str: The public CDN URL of the uploaded file, or an error message.

    Examples:
        - "Upload this image and schedule it with the tweet"
        - "Upload the video and post it to LinkedIn"
    """
    if not os.path.isfile(file_path):
        return f"Error: File not found at {file_path!r}"

    try:
        url = await _upload_to_r2(file_path)
        return url
    except ValueError as e:
        return f"Configuration Error: {e}\nSet the required CLOUDFLARE_* env vars."
    except RuntimeError as e:
        return f"Upload Error: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


# ---- Reply Links (Twitter API v2) --------------------------------------------

TWITTER_API_BASE = "https://api.x.com/2"
# Set BUFFER_TWITTER_USERNAME to enable reply-link lookups (buffer_get_reply_links).
TWITTER_USERNAME = os.environ.get("BUFFER_TWITTER_USERNAME", "")


class ReplyLinksInput(BaseModel):
    """Input for fetching tweet URLs for recent posts from the configured account."""
    model_config = ConfigDict(extra="forbid")

    count: int = Field(
        default=10,
        description="Number of recent tweets to fetch (max 100).",
        ge=1,
        le=100,
    )
    match_text: Optional[str] = Field(
        default=None,
        description="Optional text snippet to filter tweets by content match. "
        "Returns only tweets containing this substring (case-insensitive).",
    )
    username: Optional[str] = Field(
        default=None,
        description=(
            "Twitter username to look up (without @). If omitted, uses the "
            "BUFFER_TWITTER_USERNAME environment variable."
        ),
    )


async def _twitter_api_request(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
) -> dict:
    """Make authenticated request to Twitter API v2."""
    token = os.environ.get("TWITTER_BEARER_TOKEN", "")
    if not token:
        return {"error": "TWITTER_BEARER_TOKEN environment variable not set. "
                "Set it to use reply link features."}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        url = f"{TWITTER_API_BASE}/{endpoint}"
        response = await client.get(url, headers=headers, params=params or {})

        if response.status_code == 401:
            return {"error": "Twitter auth failed. Check TWITTER_BEARER_TOKEN."}
        if response.status_code == 403:
            return {"error": "Twitter API forbidden. Your API tier may lack access."}
        if response.status_code == 429:
            return {"error": "Twitter rate limit hit. Wait a few minutes and retry."}
        if response.status_code != 200:
            return {"error": f"Twitter API returned {response.status_code}: {response.text[:300]}"}

        return response.json()


async def _get_twitter_user_id(username: str) -> str:
    """Resolve Twitter username to user ID."""
    result = await _twitter_api_request(
        f"users/by/username/{username}",
        params={"user.fields": "id"},
    )
    if "error" in result:
        return ""
    try:
        return result["data"]["id"]
    except (KeyError, TypeError):
        return ""


@mcp.tool()
async def buffer_get_reply_links(input: ReplyLinksInput) -> str:
    """
    Fetch recent tweets with their URLs for reply/thread follow-up.

    Returns tweet URLs (https://x.com/{username}/status/{id}) with text
    previews and engagement counts. Useful after posting to get direct links
    for reply threads.

    Requires TWITTER_BEARER_TOKEN environment variable.
    Set BUFFER_TWITTER_USERNAME (or pass username in the call) to specify
    which account to look up.
    """
    target_username = input.username or TWITTER_USERNAME
    if not target_username:
        return (
            "Error: No Twitter username specified. Set BUFFER_TWITTER_USERNAME "
            "env var or pass `username` in the tool call."
        )

    # Step 1: Get user ID
    user_id = await _get_twitter_user_id(target_username)
    if not user_id:
        return (f"Error: Could not resolve @{target_username} user ID. "
                "Check TWITTER_BEARER_TOKEN is set and valid.")

    # Step 2: Fetch recent tweets with IDs
    params = {
        "max_results": min(input.count, 100),
        "tweet.fields": "created_at,public_metrics,text",
        "exclude": "retweets",
    }
    result = await _twitter_api_request(f"users/{user_id}/tweets", params=params)

    if "error" in result:
        return f"Error: {result['error']}"

    tweets = result.get("data", [])
    if not tweets:
        return f"No recent tweets found for @{target_username}."

    # Step 3: Filter by text match if provided
    if input.match_text:
        match_lower = input.match_text.lower()
        tweets = [t for t in tweets if match_lower in t.get("text", "").lower()]
        if not tweets:
            return (f"No tweets matching '{input.match_text}' found in the "
                    f"last {input.count} tweets from @{target_username}.")

    # Step 4: Format output
    lines = [f"Found {len(tweets)} tweet(s) from @{target_username}:\n"]
    for i, tweet in enumerate(tweets, 1):
        tweet_id = tweet.get("id", "unknown")
        text = tweet.get("text", "")
        created = tweet.get("created_at", "")
        metrics = tweet.get("public_metrics", {})

        # Truncate text for preview
        preview = text[:120] + ("..." if len(text) > 120 else "")

        url = f"https://x.com/{target_username}/status/{tweet_id}"

        lines.append(f"---\n{i}. {preview}")
        lines.append(f"   URL: {url}")
        if created:
            lines.append(f"   Posted: {created}")
        if metrics:
            parts = []
            if metrics.get("like_count", 0): parts.append(f"{metrics['like_count']} likes")
            if metrics.get("retweet_count", 0): parts.append(f"{metrics['retweet_count']} RTs")
            if metrics.get("reply_count", 0): parts.append(f"{metrics['reply_count']} replies")
            if metrics.get("impression_count", 0): parts.append(f"{metrics['impression_count']} views")
            if parts:
                lines.append(f"   Engagement: {', '.join(parts)}")

    return "\n".join(lines)


# ---- Engagement Tracking -----------------------------------------------------
# Pull post-publish engagement metrics. Buffer is the source of truth for
# which posts went out and their externalLink. Engagement metrics come from
# each platform's own API (Twitter v2 already wired; LinkedIn requires
# Marketing API access — gated on paid plan + app approval).
#
#   buffer_list_published_posts → [{post_id, channel, sent_at, text, external_link, platform_id}]
#   buffer_get_engagement(post_id) → {... + engagement: {likes, replies, impressions, ...}}


class ListPublishedPostsInput(BaseModel):
    """Input model for listing published (sent) Buffer posts."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    since: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 datetime. Only posts with sentAt at or after this value "
            "are returned. If omitted, returns the most recent `limit` posts."
        ),
    )
    channel_id: Optional[str] = Field(
        default=None,
        description="Filter to a channel. Shorthand ('twitter', 'linkedin') or raw ID.",
    )
    limit: Optional[int] = Field(
        default=50,
        description="Maximum number of posts to return (1 to 100).",
        ge=1, le=100,
    )

    @field_validator("channel_id")
    @classmethod
    def resolve_channel(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return _resolve_channel_id(v)
        return v


@mcp.tool(
    name="buffer_list_published_posts",
    annotations={
        "title": "List Published Buffer Posts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_list_published_posts(params: ListPublishedPostsInput) -> str:
    """
    List Buffer posts that have already been published (status='sent').

    Returns a JSON array with one record per post: Buffer post_id, channel,
    sent_at, text, external_link (the URL on the platform), and platform_id
    (extracted tweet_id or LinkedIn share_id). Use this to join Buffer posts
    to platform-native analytics via buffer_get_engagement.

    Args:
        params (ListPublishedPostsInput): channel_id, since, limit.

    Returns:
        str: JSON object with `posts` array and `count`.

    Examples:
        - "What did we publish last week?"
        - Engagement-pull script joining Buffer posts to Twitter API metrics.
    """
    try:
        filters: Dict[str, Any] = {"status": "sent"}
        if params.channel_id:
            filters["channelIds"] = [params.channel_id]

        query = """
        query ListSent($input: PostsInput!, $first: Int) {
            posts(input: $input, first: $first) {
                edges {
                    node {
                        id
                        status
                        sentAt
                        text
                        externalLink
                        channelId
                        channelService
                    }
                }
            }
        }
        """
        variables = {
            "input": {"organizationId": ORG_ID, "filter": filters},
            "first": params.limit or 50,
        }
        result = await _graphql_request(query, variables)

        err = _check_graphql_errors(result)
        if err:
            return json.dumps({"error": err, "posts": [], "count": 0})

        edges = result.get("data", {}).get("posts", {}).get("edges", [])
        records: List[Dict[str, Any]] = []
        for edge in edges:
            node = edge.get("node", {}) or {}
            sent_at = node.get("sentAt") or ""
            if params.since and sent_at and sent_at < params.since:
                continue
            external_link = node.get("externalLink")
            channel_service = node.get("channelService", "") or ""
            records.append({
                "post_id": node.get("id"),
                "channel": channel_service,
                "channel_id": node.get("channelId"),
                "sent_at": sent_at,
                "text": node.get("text", ""),
                "external_link": external_link,
                "platform_id": _extract_platform_id(external_link, channel_service),
            })

        return json.dumps({"posts": records, "count": len(records)}, indent=2)

    except Exception as e:
        return json.dumps({"error": _handle_api_error(e), "posts": [], "count": 0})


class GetEngagementInput(BaseModel):
    """Input model for fetching engagement on a single Buffer post."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    post_id: str = Field(
        ...,
        description="The Buffer post ID (status must be 'sent').",
        min_length=1,
    )


@mcp.tool(
    name="buffer_get_engagement",
    annotations={
        "title": "Get Engagement for a Buffer Post",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def buffer_get_engagement(params: GetEngagementInput) -> str:
    """
    Fetch engagement metrics for a single published Buffer post.

    Resolves the Buffer post via GraphQL, extracts the platform-native ID
    from externalLink, and pulls engagement from the appropriate platform:
      - Twitter / X: public_metrics + organic_metrics (likes, retweets,
        replies, impressions, quotes, bookmarks, link clicks).
      - LinkedIn: not yet wired (requires LinkedIn Marketing API access).
        Captures externalLink so backfill is possible once access is granted.

    Args:
        params (GetEngagementInput): post_id.

    Returns:
        str: JSON object with Buffer metadata + engagement payload + a
        `engagement_source` tag and `engagement_note` for transparency.

    Examples:
        - "What did the Edu Portal launch tweet do on engagement?"
        - Daily engagement_pull.py loop fanning out across all sent posts.
    """
    try:
        # Pull recent sent posts and find the requested one. Buffer's
        # `post(input: {id})` query exists but listing is faster + matches
        # the same path the listing tool uses.
        query = """
        query Find($input: PostsInput!, $first: Int) {
            posts(input: $input, first: $first) {
                edges {
                    node {
                        id sentAt text externalLink channelId channelService
                    }
                }
            }
        }
        """
        result = await _graphql_request(
            query,
            {"input": {"organizationId": ORG_ID, "filter": {"status": "sent"}}, "first": 100},
        )
        err = _check_graphql_errors(result)
        if err:
            return json.dumps({"error": err})

        edges = result.get("data", {}).get("posts", {}).get("edges", [])
        match = None
        for edge in edges:
            node = edge.get("node", {}) or {}
            if node.get("id") == params.post_id:
                match = node
                break
        if not match:
            return json.dumps({
                "error": f"Buffer post {params.post_id} not found in last 100 sent posts.",
                "post_id": params.post_id,
            })

        external_link = match.get("externalLink")
        channel_service = (match.get("channelService") or "").lower()
        platform_id = _extract_platform_id(external_link, channel_service)

        record: Dict[str, Any] = {
            "post_id": params.post_id,
            "channel": channel_service,
            "channel_id": match.get("channelId"),
            "sent_at": match.get("sentAt"),
            "text": match.get("text"),
            "external_link": external_link,
            "platform_id": platform_id,
            "engagement": None,
            "engagement_source": None,
            "engagement_note": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        if channel_service in ("twitter", "x") and platform_id:
            # organic_metrics + non_public_metrics require OAuth 1.0a User Context;
            # App-only Bearer (Basic tier) only sees public_metrics. Verified
            # via field-auth-error probe 2026-04-27.
            tw = await _twitter_api_request(
                f"tweets/{platform_id}",
                params={
                    "tweet.fields": "public_metrics,created_at",
                },
            )
            if "error" in tw:
                record["engagement_source"] = "twitter_api_v2_error"
                record["engagement_note"] = tw["error"]
            else:
                t = tw.get("data", {}) or {}
                pm = t.get("public_metrics", {}) or {}
                om = t.get("organic_metrics", {}) or {}
                npm = t.get("non_public_metrics", {}) or {}
                record["engagement"] = {
                    "likes": pm.get("like_count"),
                    "retweets": pm.get("retweet_count"),
                    "replies": pm.get("reply_count"),
                    "quotes": pm.get("quote_count"),
                    "bookmarks": pm.get("bookmark_count"),
                    "impressions": (
                        pm.get("impression_count")
                        or om.get("impression_count")
                        or npm.get("impression_count")
                    ),
                    "url_link_clicks": (
                        om.get("url_link_clicks") or npm.get("url_link_clicks")
                    ),
                    "user_profile_clicks": (
                        om.get("user_profile_clicks") or npm.get("user_profile_clicks")
                    ),
                }
                record["engagement_source"] = "twitter_api_v2"
        elif channel_service == "linkedin":
            record["engagement_source"] = "linkedin_blocked"
            record["engagement_note"] = (
                "LinkedIn engagement requires LinkedIn Marketing API access "
                "(paid plan + app approval). Capture-only mode: external_link "
                "is logged so engagement can be backfilled once access lands."
            )
        else:
            record["engagement_source"] = "unsupported"
            record["engagement_note"] = f"Unsupported channel: {channel_service}"

        return json.dumps(record, indent=2)

    except Exception as e:
        return json.dumps({"error": _handle_api_error(e), "post_id": params.post_id})


# ---- Entry Point -------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
