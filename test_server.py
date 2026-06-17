#!/usr/bin/env python3
"""
Unit tests for buffer-mcp/server.py.

These tests mock _graphql_request so no real Buffer API calls are made.
Coverage focuses on the two bugs from the 2026-04-27 daily-social-drafter run:

  Bug 1: post-ID rotation between createPost and posts() queries.
         Fix: re-list after create, return canonical (list-query) ID.

  Bug 2: buffer_list_posts truncated to 1 post per channel because the
         posts(input) query passed no `first` arg and pageInfo cursors
         were not followed.

Run:  python3 -m unittest buffer-mcp.test_server -v
      (or, from inside buffer-mcp/:  python3 -m unittest test_server -v)
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import patch
from typing import Any, Dict, List, Optional

# Buffer token must be present (any value) so the module imports cleanly.
os.environ.setdefault("BUFFER_ACCESS_TOKEN", "test-token")

# Allow running from buffer-mcp/ or repo root.
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

import server  # noqa: E402
from server import (  # noqa: E402
    _canonical_post_id,
    _due_ats_match,
    _is_pagination_schema_error,
    _list_posts_paginated,
    _parse_iso,
    buffer_create_post,
    buffer_delete_post,
    buffer_list_posts,
    buffer_find_post_by_schedule,
    CreatePostInput,
    DeletePostInput,
    FindPostByScheduleInput,
    ListPostsInput,
)


def run_async(coro):
    """Helper: run an awaitable synchronously inside a unittest method."""
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeGraphQL:
    """
    Stand-in for _graphql_request that returns scripted responses.

    Each call inspects the (query, variables) tuple and pops the next
    matching response off `script`. Scripts are matched in order.
    """

    def __init__(self, script: List[Dict[str, Any]]):
        # Each script entry has either {"any": True, "response": {...}}
        # or {"match": fn, "response": {...}}.
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, query: str, variables: Optional[Dict[str, Any]] = None,
                       max_retries: int = 3):
        self.calls.append({"query": query, "variables": variables})
        if not self.script:
            raise AssertionError(
                f"FakeGraphQL ran out of scripted responses. "
                f"Unexpected call #{len(self.calls)}: query starts with "
                f"{query.strip().splitlines()[0] if query else '?'}"
            )
        entry = self.script.pop(0)
        match_fn = entry.get("match")
        if match_fn and not match_fn(query, variables):
            raise AssertionError(
                f"FakeGraphQL: scripted entry did not match call #{len(self.calls)}."
            )
        return entry["response"]


# ---- Tiny helpers --------------------------------------------------------


def _post_node(post_id: str, due_at: str, text: str = "hello", status: str = "scheduled"):
    return {"id": post_id, "status": status, "dueAt": due_at, "text": text}


def _list_response(nodes, end_cursor: Optional[str] = None, has_next: bool = False):
    edges = [{"cursor": f"c{i}", "node": n} for i, n in enumerate(nodes)]
    return {
        "data": {
            "posts": {
                "edges": edges,
                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
            }
        }
    }


def _create_success(post_id: str, due_at: str, text: str = "hello"):
    return {
        "data": {
            "createPost": {
                "__typename": "PostActionSuccess",
                "post": _post_node(post_id, due_at, text=text),
            }
        }
    }


def _delete_success(post_id: str):
    return {
        "data": {
            "deletePost": {"__typename": "DeletePostSuccess", "id": post_id}
        }
    }


def _delete_not_found():
    return {
        "data": {
            "deletePost": {
                "__typename": "VoidMutationError",
                "message": "Document not found",
            }
        }
    }


# ---- Helper-level tests --------------------------------------------------


class TestHelpers(unittest.TestCase):

    def test_canonical_post_id_extracts_id(self):
        self.assertEqual(_canonical_post_id({"id": "abc123"}), "abc123")
        self.assertEqual(_canonical_post_id({}), "")
        self.assertEqual(_canonical_post_id({"id": None}), "")

    def test_parse_iso_handles_z_and_offset(self):
        a = _parse_iso("2026-04-27T19:30:00Z")
        b = _parse_iso("2026-04-27T19:30:00+00:00")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a, b)
        self.assertIsNone(_parse_iso(""))
        self.assertIsNone(_parse_iso("not a date"))

    def test_due_ats_match_within_tolerance(self):
        self.assertTrue(_due_ats_match("2026-04-27T19:30:00Z", "2026-04-27T19:30:30Z"))
        self.assertTrue(_due_ats_match("2026-04-27T19:30:00Z", "2026-04-27T19:31:00Z"))
        self.assertFalse(_due_ats_match("2026-04-27T19:30:00Z", "2026-04-27T19:35:00Z"))
        self.assertFalse(_due_ats_match("", "2026-04-27T19:30:00Z"))

    def test_pagination_schema_error_detector(self):
        self.assertTrue(_is_pagination_schema_error('Field "first" is not defined'))
        self.assertTrue(_is_pagination_schema_error("Unknown argument 'after' on PostsInput"))
        self.assertFalse(_is_pagination_schema_error("FORBIDDEN"))
        self.assertFalse(_is_pagination_schema_error(""))


# ---- Bug 2: pagination ---------------------------------------------------


class TestListPostsPagination(unittest.TestCase):
    """Bug 2 regression coverage: list_posts must follow cursors."""

    def test_paginated_returns_all_pages_until_limit(self):
        nodes_a = [_post_node(f"id-{i}", "2026-05-01T10:00:00Z") for i in range(5)]
        nodes_b = [_post_node(f"id-{i+5}", "2026-05-01T11:00:00Z") for i in range(5)]
        fake = FakeGraphQL([
            {"any": True, "response": _list_response(nodes_a, end_cursor="cursor-a", has_next=True)},
            {"any": True, "response": _list_response(nodes_b, end_cursor=None, has_next=False)},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(_list_posts_paginated(channel_id="twitter", target_limit=50))

        self.assertEqual(len(got), 10, "must return both pages, not just the first")
        self.assertEqual([n["id"] for n in got], [f"id-{i}" for i in range(10)])

        # And it must have actually issued a follow-up call with `after`.
        self.assertEqual(len(fake.calls), 2)
        second = fake.calls[1]["variables"]["input"]
        self.assertEqual(second.get("after"), "cursor-a")
        self.assertEqual(second.get("first"), 50)

    def test_paginated_respects_target_limit(self):
        many = [_post_node(f"id-{i}", "2026-05-01T10:00:00Z") for i in range(50)]
        fake = FakeGraphQL([
            {"any": True, "response": _list_response(many, end_cursor="cursor-x", has_next=True)},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(_list_posts_paginated(channel_id="twitter", target_limit=5))

        self.assertEqual(len(got), 5)

    def test_falls_back_to_legacy_query_on_schema_error(self):
        nodes = [_post_node("id-fallback", "2026-05-01T10:00:00Z")]
        fake = FakeGraphQL([
            # First call: paginated shape rejected by Buffer.
            {"any": True, "response": {
                "errors": [{"message": 'Field "first" is not defined on PostsInput'}]
            }},
            # Retry: legacy shape (no first/after) succeeds.
            {"any": True, "response": _list_response(nodes)},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(_list_posts_paginated(channel_id="twitter", target_limit=50))

        self.assertEqual([n["id"] for n in got], ["id-fallback"])
        # Confirm second call dropped pagination args.
        legacy_input = fake.calls[1]["variables"]["input"]
        self.assertNotIn("first", legacy_input)
        self.assertNotIn("after", legacy_input)

    def test_buffer_list_posts_returns_more_than_one(self):
        """End-to-end regression: list_posts must return > 1 post per channel
        when many are scheduled, not just the most recent."""
        nodes = [_post_node(f"tw-{i}", f"2026-05-0{i+1}T10:00:00Z") for i in range(9)]
        fake = FakeGraphQL([
            {"any": True, "response": _list_response(nodes)},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_list_posts(ListPostsInput(channel_id="twitter", limit=50)))

        # Confirm all 9 IDs appear in the output, not just one.
        for i in range(9):
            self.assertIn(f"tw-{i}", got)


# ---- Bug 1: ID rotation --------------------------------------------------


class TestCreatePostCanonicalId(unittest.TestCase):
    """Bug 1 regression coverage: create-time ID can rotate; tool must
    return the canonical (list-query) ID after create."""

    def test_create_returns_list_query_id_when_it_differs(self):
        due_at = "2026-05-15T19:30:00Z"
        # Buffer's createPost returns the transient (pre-stage) ObjectID...
        # ...and a moments-later posts() returns a different canonical one.
        fake = FakeGraphQL([
            {"any": True, "response": _create_success("create-id-X", due_at, text="hello world")},
            {"any": True, "response": _list_response(
                [_post_node("canonical-id-Y", due_at, text="hello world")]
            )},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_create_post(CreatePostInput(
                channel_id="twitter",
                text="hello world",
                scheduled_at=due_at,
            )))

        self.assertIn("canonical-id-Y", got, "must surface the list-query ID")
        self.assertIn("create-time ID was `create-id-X`", got,
                      "must transparently note the rotation")

    def test_create_falls_back_to_create_id_when_relist_empty(self):
        due_at = "2026-05-15T19:30:00Z"
        fake = FakeGraphQL([
            {"any": True, "response": _create_success("create-id-X", due_at, text="solo post")},
            # Re-list returns nothing (race condition: post not visible yet).
            {"any": True, "response": _list_response([])},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_create_post(CreatePostInput(
                channel_id="twitter",
                text="solo post",
                scheduled_at=due_at,
            )))

        self.assertIn("create-id-X", got, "must fall back to create-time ID")
        self.assertNotIn("Canonical ID source", got,
                         "no rotation note when no canonical was found")

    def test_create_with_share_now_skips_relist(self):
        # share_now posts publish immediately; no scheduled-list-match window.
        fake = FakeGraphQL([
            {"any": True, "response": _create_success("share-id", "2026-05-15T19:30:00Z")},
            # If the tool tries to re-list, the script will be exhausted and fail.
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_create_post(CreatePostInput(
                channel_id="twitter",
                text="hi",
                share_now=True,
            )))

        self.assertIn("share-id", got)
        self.assertEqual(len(fake.calls), 1, "share_now must NOT trigger a re-list")


# ---- Bug 1: delete by verify-by-schedule --------------------------------


class TestDeletePostVerify(unittest.TestCase):

    def test_delete_with_verify_uses_canonical_id(self):
        due_at = "2026-05-15T19:30:00Z"
        # Caller has stale `create-id-X`; verify-by-schedule should look up
        # `canonical-id-Y` and delete THAT.
        fake = FakeGraphQL([
            # _find_post_by_schedule call (paginated list).
            {"any": True, "response": _list_response(
                [_post_node("canonical-id-Y", due_at, text="hello world")]
            )},
            # deletePost mutation against the canonical ID.
            {"any": True, "response": _delete_success("canonical-id-Y")},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_delete_post(DeletePostInput(
                post_id="create-id-X",
                expected_scheduled_at=due_at,
                verify_channel_id="twitter",
                verify_text_prefix="hello world",
            )))

        self.assertIn("canonical-id-Y", got)
        self.assertIn("verify-by-schedule", got)
        # The delete mutation must have been called with the canonical ID.
        delete_call_input = fake.calls[1]["variables"]["input"]
        self.assertEqual(delete_call_input["id"], "canonical-id-Y")

    def test_delete_without_verify_args_uses_provided_id(self):
        fake = FakeGraphQL([
            {"any": True, "response": _delete_success("plain-id")},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_delete_post(DeletePostInput(post_id="plain-id")))

        self.assertIn("plain-id", got)
        self.assertNotIn("verify-by-schedule", got)


# ---- buffer_find_post_by_schedule tool ----------------------------------


class TestFindPostByScheduleTool(unittest.TestCase):

    def test_returns_canonical_match(self):
        due_at = "2026-05-15T19:30:00Z"
        nodes = [
            _post_node("not-the-one", "2026-05-15T20:00:00Z", text="other"),
            _post_node("canonical-id", due_at, text="target text 12345"),
        ]
        fake = FakeGraphQL([
            {"any": True, "response": _list_response(nodes)},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_find_post_by_schedule(FindPostByScheduleInput(
                channel_id="twitter",
                scheduled_at=due_at,
                text_prefix="target text 12345",
            )))

        self.assertIn("canonical-id", got)

    def test_returns_not_found_message_when_no_match(self):
        fake = FakeGraphQL([
            {"any": True, "response": _list_response([])},
        ])
        with patch.object(server, "_graphql_request", new=fake):
            got = run_async(buffer_find_post_by_schedule(FindPostByScheduleInput(
                channel_id="twitter",
                scheduled_at="2026-05-15T19:30:00Z",
            )))

        self.assertIn("No scheduled post found", got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
