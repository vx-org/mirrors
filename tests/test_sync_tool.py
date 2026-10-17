"""Offline release-lookup regressions. Never invoke gh, curl, or a live sync."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import unquote


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync-tool.py"
SPEC = importlib.util.spec_from_file_location("sync_tool", SCRIPT)
sync_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_tool)


def json_result(data):
    return subprocess.CompletedProcess([], 0, json.dumps(data), "")


def graphql_result(release=None):
    return json_result({"data": {"repository": {"release": release}}})


def http_result(status, data, *, newline="\n"):
    output = newline.join([
        f"HTTP/2.0 {status} Response",
        "Content-Type: application/json",
        "",
        json.dumps(data),
    ])
    return subprocess.CompletedProcess(
        [], 0 if status == 200 else 1, output,
        "" if status == 200 else f"gh: API error (HTTP {status})",
    )


class ReleaseLookupTests(unittest.TestCase):
    REPO = "vx-org/mirrors"

    def setUp(self):
        sync_tool._MIRROR_TAGS_CACHE.clear()
        sync_tool._MIRROR_MISSING_TAGS_CACHE.clear()
        self.run = self.enterContext(patch.object(sync_tool.subprocess, "run"))
        self.enterContext(patch("time.sleep"))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_archive_over_10000_never_requests_page_101(self):
        archive = [f"tool-{version}" for version in range(10001)]

        def respond(command, **kwargs):
            if command[-1].endswith("releases?per_page=100&page=1"):
                return json_result([{"tag_name": tag} for tag in archive[:100]])
            self.assertEqual(command[-1], f"repos/{self.REPO}/releases/tags/tool-10000")
            return http_result(200, {"tag_name": archive[-1]})

        self.run.side_effect = respond
        self.assertTrue(sync_tool.release_exists(self.REPO, archive[0]))
        self.assertTrue(sync_tool.release_exists(self.REPO, archive[-1]))
        self.assertTrue(sync_tool.release_exists(self.REPO, archive[-1]))
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(len(sync_tool._MIRROR_TAGS_CACHE[self.REPO]), 101)

    def test_359_uncached_versions_cost_one_prefetch_plus_one_each(self):
        def respond(command, **kwargs):
            if command[-1].endswith("releases?per_page=100&page=1"):
                return json_result([])
            tag = unquote(command[-1].rsplit("/", 1)[-1])
            return http_result(200, {"tag_name": tag})

        self.run.side_effect = respond
        for _ in range(2):
            for version in range(359):
                self.assertTrue(sync_tool.release_exists(self.REPO, f"cargo-nextest-{version}"))
        self.assertEqual(self.run.call_count, 360)

    def test_exact_404_is_cached_even_after_short_list(self):
        self.run.side_effect = [json_result([]), http_result(404, {"message": "Not Found"}), graphql_result()]
        self.assertFalse(sync_tool.release_exists(self.REPO, "new-tag"))
        self.assertFalse(sync_tool.release_exists(self.REPO, "new-tag"))
        self.assertEqual(self.run.call_count, 3)

    def test_old_draft_outside_prefetch_is_cached_as_present(self):
        self.run.side_effect = [
            json_result([{"tag_name": f"recent-{n}"} for n in range(100)]),
            http_result(404, {"message": "Not Found"}),
            graphql_result({"tagName": "old-draft", "isDraft": True}),
        ]
        self.assertTrue(sync_tool.release_exists(self.REPO, "old-draft"))
        self.assertTrue(sync_tool.release_exists(self.REPO, "old-draft"))
        self.assertNotIn("old-draft", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])
        self.assertEqual(self.run.call_count, 3)

    def test_graphql_also_accepts_release_published_after_rest_404(self):
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}),
            graphql_result({"tagName": "new", "isDraft": False}),
        ]
        self.assertTrue(sync_tool.release_exists(self.REPO, "new"))

    def test_graphql_uses_literal_string_variables(self):
        tag = "@tag/{repo}"
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}),
            graphql_result({"tagName": tag, "isDraft": True}),
        ]
        self.assertTrue(sync_tool.release_exists(self.REPO, tag))
        command = self.run.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "api", "graphql"])
        self.assertEqual(command[-6:], ["-f", "owner=vx-org", "-f", "name=mirrors", "-f", f"tagName={tag}"])
        self.assertNotIn("-F", command)  # no type conversion, placeholders, or @file reads

    def test_359_true_misses_cost_one_prefetch_plus_two_each(self):
        def respond(command, **kwargs):
            if command[2] == "graphql":
                return graphql_result()
            if command[-1].endswith("releases?per_page=100&page=1"):
                return json_result([])
            return http_result(404, {"message": "Not Found"})

        self.run.side_effect = respond
        for _ in range(2):
            for version in range(359):
                self.assertFalse(sync_tool.release_exists(self.REPO, f"cargo-nextest-{version}"))
        self.assertEqual(self.run.call_count, 719)

    def test_graphql_errors_with_partial_data_are_not_absence(self):
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}),
            json_result({"errors": [{"message": "Resource access denied"}],
                         "data": {"repository": {"release": None}}}),
            http_result(404, {"message": "Not Found"}),
            graphql_result({"tagName": "old", "isDraft": True}),
        ]
        with self.assertRaisesRegex(RuntimeError, "Resource access denied"):
            sync_tool.release_exists(self.REPO, "old")
        self.assertNotIn("old", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])
        self.assertTrue(sync_tool.release_exists(self.REPO, "old"))
        self.assertEqual(self.run.call_count, 5)

    def test_invalid_graphql_data_is_not_absence(self):
        invalid = [
            None, [], {}, {"data": None}, {"data": {"repository": None}},
            {"data": {"repository": {}}}, {"data": {"repository": []}},
            {"data": {"repository": {"release": {}}}},
            {"data": {"repository": {"release": {"tagName": "wrong", "isDraft": True}}}},
            {"data": {"repository": {"release": {"tagName": "old", "isDraft": "true"}}}},
            {"errors": [{"message": "Query failed"}]},
        ]
        for data in invalid:
            with self.subTest(data=data):
                sync_tool._MIRROR_TAGS_CACHE[self.REPO] = set()
                self.run.side_effect = [http_result(404, {"message": "Not Found"}), json_result(data)]
                with self.assertRaises(RuntimeError):
                    sync_tool.release_exists(self.REPO, "old")
                self.assertNotIn("old", sync_tool._MIRROR_TAGS_CACHE[self.REPO])
                self.assertNotIn("old", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])

    def test_malformed_graphql_json_is_not_absence(self):
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}),
            subprocess.CompletedProcess([], 0, "not JSON", ""),
        ]
        with self.assertRaises(json.JSONDecodeError):
            sync_tool.release_exists(self.REPO, "old")
        self.assertNotIn("old", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])

    def test_graphql_cli_error_details_are_preserved(self):
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}),
            subprocess.CompletedProcess([], 1, "", "GraphQL: Resource not accessible by integration"),
        ]
        with self.assertRaisesRegex(sync_tool.GhCommandError, "Resource not accessible by integration"):
            sync_tool.release_exists(self.REPO, "old")
        self.assertNotIn("old", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])

    def test_draft_lookup_cache_is_repository_specific(self):
        other = "another/mirror"
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}),
            graphql_result({"tagName": "shared", "isDraft": True}),
            json_result([]), http_result(404, {"message": "Not Found"}), graphql_result(),
        ]
        self.assertTrue(sync_tool.release_exists(self.REPO, "shared"))
        self.assertFalse(sync_tool.release_exists(other, "shared"))
        self.assertTrue(sync_tool.release_exists(self.REPO, "shared"))
        self.assertFalse(sync_tool.release_exists(other, "shared"))
        self.assertEqual(self.run.call_count, 6)

    def test_cached_recent_tag_needs_no_exact_lookup(self):
        self.run.return_value = json_result([{"tag_name": "recent"}])
        self.assertTrue(sync_tool.release_exists(self.REPO, "recent"))
        self.assertTrue(sync_tool.release_exists(self.REPO, "recent"))
        self.assertEqual(self.run.call_count, 1)

    def test_tag_is_encoded_as_one_path_segment(self):
        tag = "tool/release+1#test?value=% ü{repo}"
        self.run.side_effect = [json_result([]), http_result(200, {"tag_name": tag})]
        self.assertTrue(sync_tool.release_exists(self.REPO, tag))
        self.assertEqual(self.run.call_args.args[0], [
            "gh", "api", "--include",
            f"repos/{self.REPO}/releases/tags/"
            "tool%2Frelease%2B1%23test%3Fvalue%3D%25%20%C3%BC%7Brepo%7D",
        ])

    def test_crlf_http_response(self):
        self.run.side_effect = [json_result([]), http_result(200, {"tag_name": "old"}, newline="\r\n")]
        self.assertTrue(sync_tool.release_exists(self.REPO, "old"))

    def test_repository_caches_are_isolated(self):
        other = "another/mirror"
        self.run.side_effect = [
            json_result([{"tag_name": "shared"}]),
            json_result([]),
            http_result(404, {"message": "Not Found"}),
            graphql_result(),
        ]
        self.assertTrue(sync_tool.release_exists(self.REPO, "shared"))
        self.assertFalse(sync_tool.release_exists(other, "shared"))
        self.assertTrue(sync_tool.release_exists(self.REPO, "shared"))
        self.assertEqual(self.run.call_count, 4)

    def test_mark_created_clears_only_target_repository_miss(self):
        other = "another/mirror"
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}), graphql_result(),
            json_result([]), http_result(404, {"message": "Not Found"}), graphql_result(),
        ]
        self.assertFalse(sync_tool.release_exists(self.REPO, "new"))
        self.assertFalse(sync_tool.release_exists(other, "new"))
        sync_tool.mark_release_created("new", other)
        self.assertTrue(sync_tool.release_exists(other, "new"))
        self.assertFalse(sync_tool.release_exists(self.REPO, "new"))
        self.assertEqual(self.run.call_count, 6)

    def test_mark_created_preserves_single_argument_call(self):
        self.run.side_effect = [json_result([]), http_result(404, {"message": "Not Found"}), graphql_result()]
        self.assertFalse(sync_tool.release_exists(self.REPO, "new"))
        sync_tool.mark_release_created("new")
        self.assertTrue(sync_tool.release_exists(self.REPO, "new"))
        self.assertEqual(self.run.call_count, 3)

    def test_create_helper_marks_the_actual_repository(self):
        other = "another/mirror"
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}), graphql_result(),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
        self.assertFalse(sync_tool.release_exists(other, "new"))
        self.assertEqual(sync_tool._create_mirror_release("tool", "1", "new", other, [], "", False), "ok")
        self.assertTrue(sync_tool.release_exists(other, "new"))
        self.assertNotIn(self.REPO, sync_tool._MIRROR_TAGS_CACHE)
        self.assertEqual(self.run.call_count, 4)

    def test_failed_or_dry_creation_does_not_mark_present(self):
        self.run.side_effect = [
            json_result([]), http_result(404, {"message": "Not Found"}), graphql_result(),
            subprocess.CompletedProcess([], 1, "", "creation failed"),
        ]
        self.assertFalse(sync_tool.release_exists(self.REPO, "new"))
        args = ("tool", "1", "new", self.REPO, [], "")
        self.assertEqual(sync_tool._create_mirror_release(*args, True), "dry")
        self.assertFalse(sync_tool.release_exists(self.REPO, "new"))
        self.assertEqual(sync_tool._create_mirror_release(*args, False), "fail")
        self.assertFalse(sync_tool.release_exists(self.REPO, "new"))
        self.assertEqual(self.run.call_count, 4)

    def test_exact_api_errors_propagate_and_are_not_cached(self):
        for status in (401, 403, 422, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                sync_tool._MIRROR_TAGS_CACHE.clear()
                self.run.reset_mock()
                error = http_result(status, {"message": "API error"})
                self.run.side_effect = lambda command, **kwargs: (
                    json_result([]) if "?per_page=" in command[-1] else error
                )
                with self.assertRaises(RuntimeError):
                    sync_tool.release_exists(self.REPO, "unknown")
                self.assertNotIn("unknown", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])
                self.assertNotIn("unknown", sync_tool._MIRROR_TAGS_CACHE[self.REPO])
                self.assertLessEqual(self.run.call_count, 7)  # one list + up to six retries

    def test_list_api_errors_never_publish_partial_cache(self):
        for status in (403, 404, 422, 429, 500, 503):
            with self.subTest(status=status):
                self.run.side_effect = None
                self.run.return_value = http_result(status, {"message": "API error"})
                with self.assertRaises(RuntimeError):
                    sync_tool.release_exists(self.REPO, "unknown")
                self.assertNotIn(self.REPO, sync_tool._MIRROR_TAGS_CACHE)

    def test_malformed_list_is_not_cached(self):
        for data in ({}, None, [None], [{}], [{"tag_name": 1}], [{"tag_name": "valid"}, {}]):
            with self.subTest(data=data):
                self.run.return_value = json_result(data)
                with self.assertRaises(RuntimeError):
                    sync_tool.release_exists(self.REPO, "unknown")
                self.assertNotIn(self.REPO, sync_tool._MIRROR_TAGS_CACHE)

    def test_malformed_list_json_propagates(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, "not JSON", "")
        with self.assertRaises(json.JSONDecodeError):
            sync_tool.release_exists(self.REPO, "unknown")
        self.assertNotIn(self.REPO, sync_tool._MIRROR_TAGS_CACHE)

    def test_generic_cli_failure_is_not_absence(self):
        self.run.side_effect = [
            json_result([]),
            subprocess.CompletedProcess([], 1, "", "failed looking up a tag containing 404"),
        ]
        with self.assertRaises(sync_tool.GhCommandError):
            sync_tool.release_exists(self.REPO, "unknown")
        self.assertNotIn("unknown", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])

    def test_invalid_exact_success_is_not_cached(self):
        invalid = [
            "not JSON", "{}", "HTTP/2.0 200 OK\n\nnot JSON",
            http_result(200, {}).stdout,
            http_result(200, {"tag_name": "wrong"}).stdout,
            http_result(200, []).stdout,
            http_result(404, {"message": "Not Found"}).stdout,
        ]
        for output in invalid:
            with self.subTest(output=output):
                self.run.side_effect = None
                sync_tool._MIRROR_TAGS_CACHE[self.REPO] = set()
                self.run.return_value = subprocess.CompletedProcess([], 0, output, "")
                with self.assertRaises((RuntimeError, ValueError)):
                    sync_tool.release_exists(self.REPO, "unknown")
                self.assertNotIn("unknown", sync_tool._MIRROR_TAGS_CACHE[self.REPO])
                self.assertNotIn("unknown", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])

    def test_malformed_404_is_not_absence(self):
        for output in ("", "HTTP/2.0 404 Not Found\n\nnot JSON", http_result(404, {}).stdout):
            with self.subTest(output=output):
                sync_tool._MIRROR_TAGS_CACHE[self.REPO] = set()
                self.run.return_value = subprocess.CompletedProcess([], 1, output, "gh: Not Found (HTTP 404)")
                with self.assertRaises(sync_tool.GhCommandError):
                    sync_tool.release_exists(self.REPO, "unknown")
                self.assertNotIn("unknown", sync_tool._MIRROR_MISSING_TAGS_CACHE[self.REPO])


if __name__ == "__main__":
    unittest.main()
