from __future__ import annotations

import json
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from release_tools import (  # noqa: E402
    ReleaseToolError,
    GitHubClient,
    check_automatic_destination,
    automatic_notes,
    build_questbook_zip,
    ensure_manual_destination_available,
    manual_notes,
    parse_mirror_marker,
    publish_release,
    sanitize_tag,
    select_asset,
    select_releases_to_sync,
    validate_git_tag,
)


def release(
    release_id: int,
    tag: str,
    published_at: str,
    *,
    body: str = "",
    assets: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": release_id,
        "tag_name": tag,
        "name": f"Release {tag}",
        "published_at": published_at,
        "prerelease": "nightly" in tag,
        "html_url": f"https://github.com/GTNewHorizons/GT-New-Horizons-Modpack/releases/tag/{tag}",
        "body": body,
        "assets": assets or [],
    }


def marker(release_id: int, published_at: str) -> str:
    payload = json.dumps(
        {"upstream_release_id": release_id, "published_at": published_at},
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"notes\n<!-- gtnh-questbook-mirror:{payload} -->"


def asset(name: str, asset_id: int = 1) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": name,
        "size": 123,
        "browser_download_url": f"https://example.invalid/{name}",
    }


class SynchronizationTests(unittest.TestCase):
    def test_first_sync_selects_only_latest_release(self) -> None:
        upstream = [
            release(20, "old", "2026-09-08T12:00:00Z"),
            release(30, "new", "2026-09-09T12:00:00Z"),
        ]

        selected = select_releases_to_sync(upstream, [])

        self.assertEqual(["new"], [item["tag_name"] for item in selected])

    def test_later_sync_selects_every_new_release_oldest_first(self) -> None:
        upstream = [
            release(30, "third", "2026-09-09T12:00:00Z"),
            release(10, "first", "2026-09-09T10:00:00Z"),
            release(20, "second", "2026-09-09T11:00:00Z"),
        ]
        downstream = [{"body": marker(10, "2026-09-09T10:00:00Z")}]

        selected = select_releases_to_sync(upstream, downstream)

        self.assertEqual(
            ["second", "third"], [item["tag_name"] for item in selected]
        )

    def test_release_id_breaks_equal_timestamp_ties(self) -> None:
        timestamp = "2026-09-09T12:00:00Z"
        upstream = [release(12, "new", timestamp), release(10, "old", timestamp)]
        downstream = [{"body": marker(10, timestamp)}]

        selected = select_releases_to_sync(upstream, downstream)

        self.assertEqual(["new"], [item["tag_name"] for item in selected])

    def test_rerun_after_mirror_selects_nothing(self) -> None:
        timestamp = "2026-09-09T12:00:00Z"
        upstream = [release(30, "current", timestamp)]
        downstream = [{"body": marker(30, timestamp)}]

        self.assertEqual([], select_releases_to_sync(upstream, downstream))

    def test_malformed_mirror_marker_is_rejected(self) -> None:
        downstream = [{"body": '<!-- gtnh-questbook-mirror:{"bad":true} -->'}]

        with self.assertRaisesRegex(ReleaseToolError, "mirror marker"):
            select_releases_to_sync(
                [release(30, "current", "2026-09-09T12:00:00Z")], downstream
            )

    def test_parse_marker_ignores_ordinary_release_notes(self) -> None:
        self.assertIsNone(parse_mirror_marker("Manual preview release"))


class AssetSelectionTests(unittest.TestCase):
    def test_exact_tag_zip_wins_when_other_zips_exist(self) -> None:
        upstream = release(
            30,
            "v/one",
            "2026-09-09T12:00:00Z",
            assets=[asset("other.zip", 1), asset("v/one.zip", 2)],
        )

        self.assertEqual(2, select_asset(upstream)["id"])

    def test_a_sole_zip_is_accepted_when_name_differs(self) -> None:
        upstream = release(
            30,
            "v1",
            "2026-09-09T12:00:00Z",
            assets=[asset("checksums.txt", 1), asset("client-package.zip", 2)],
        )

        self.assertEqual("client-package.zip", select_asset(upstream)["name"])

    def test_no_zip_is_rejected(self) -> None:
        upstream = release(
            30,
            "v1",
            "2026-09-09T12:00:00Z",
            assets=[asset("checksums.txt")],
        )

        with self.assertRaisesRegex(ReleaseToolError, "no attached ZIP"):
            select_asset(upstream)

    def test_multiple_ambiguous_zips_are_rejected(self) -> None:
        upstream = release(
            30,
            "v1",
            "2026-09-09T12:00:00Z",
            assets=[asset("client.zip", 1), asset("server.zip", 2)],
        )

        with self.assertRaisesRegex(ReleaseToolError, "ambiguous"):
            select_asset(upstream)


class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_zip_source_includes_only_complete_questbook_tree(self) -> None:
        source = self.root / "modpack.zip"
        output = self.root / "questbook.zip"
        with zipfile.ZipFile(source, "w") as archive_file:
            archive_file.writestr(
                "config/betterquesting/DefaultQuests/Quest.json", "{}"
            )
            archive_file.writestr(
                "config/betterquesting/resources/image.png", b"image"
            )
            archive_file.writestr("config/betterquesting/questbook.cfg", "cfg")
            archive_file.writestr("mods/ignored.jar", b"ignored")

        count = build_questbook_zip(source, output)

        self.assertEqual(3, count)
        with zipfile.ZipFile(output) as archive_file:
            self.assertEqual(
                [
                    "betterquesting/DefaultQuests/Quest.json",
                    "betterquesting/questbook.cfg",
                    "betterquesting/resources/image.png",
                ],
                archive_file.namelist(),
            )

    def test_directory_source_packages_nested_files_and_excludes_siblings(self) -> None:
        source = self.root / "upstream"
        questbook = source / "config" / "betterquesting"
        (questbook / "DefaultQuests").mkdir(parents=True)
        (questbook / "DefaultQuests" / "Quest.json").write_text("{}")
        (questbook / "Readme.md").write_text("read me")
        (source / "config" / "unrelated.cfg").write_text("ignored")
        output = self.root / "questbook.zip"

        count = build_questbook_zip(source, output)

        self.assertEqual(2, count)
        with zipfile.ZipFile(output) as archive_file:
            self.assertEqual(
                [
                    "betterquesting/DefaultQuests/Quest.json",
                    "betterquesting/Readme.md",
                ],
                archive_file.namelist(),
            )

    def test_missing_questbook_content_is_rejected(self) -> None:
        source = self.root / "modpack.zip"
        output = self.root / "questbook.zip"
        with zipfile.ZipFile(source, "w") as archive_file:
            archive_file.writestr("mods/only.jar", b"ignored")

        with self.assertRaisesRegex(ReleaseToolError, "contains no files"):
            build_questbook_zip(source, output)
        self.assertFalse(output.exists())

    def test_path_traversal_is_rejected(self) -> None:
        source = self.root / "modpack.zip"
        with zipfile.ZipFile(source, "w") as archive_file:
            archive_file.writestr("../outside.txt", "unsafe")
            archive_file.writestr("config/betterquesting/questbook.cfg", "cfg")

        with self.assertRaisesRegex(ReleaseToolError, "unsafe ZIP member"):
            build_questbook_zip(source, self.root / "questbook.zip")

    def test_absolute_path_is_rejected(self) -> None:
        source = self.root / "modpack.zip"
        with zipfile.ZipFile(source, "w") as archive_file:
            archive_file.writestr("/absolute.txt", "unsafe")
            archive_file.writestr("config/betterquesting/questbook.cfg", "cfg")

        with self.assertRaisesRegex(ReleaseToolError, "unsafe ZIP member"):
            build_questbook_zip(source, self.root / "questbook.zip")

    def test_backslash_path_is_rejected(self) -> None:
        source = self.root / "modpack.zip"
        with zipfile.ZipFile(source, "w") as archive_file:
            archive_file.writestr("config/betterquesting/questbook.cfg", "unsafe")
        source.write_bytes(
            source.read_bytes().replace(
                b"config/betterquesting", b"config\\betterquesting"
            )
        )

        with self.assertRaisesRegex(ReleaseToolError, "backslash"):
            build_questbook_zip(source, self.root / "questbook.zip")

    def test_symlink_member_is_rejected(self) -> None:
        source = self.root / "modpack.zip"
        link = zipfile.ZipInfo("config/betterquesting/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(source, "w") as archive_file:
            archive_file.writestr(link, "target")

        with self.assertRaisesRegex(ReleaseToolError, "symbolic link"):
            build_questbook_zip(source, self.root / "questbook.zip")


class NamingAndNotesTests(unittest.TestCase):
    def test_filename_sanitization_preserves_safe_characters(self) -> None:
        self.assertEqual("release_branch-1.0", sanitize_tag("release/branch-1.0"))

    def test_filename_sanitization_never_returns_empty_component(self) -> None:
        self.assertEqual("release", sanitize_tag("///"))

    def test_automatic_notes_contain_marker_source_and_installation_warning(self) -> None:
        upstream = release(30, "v1", "2026-09-09T12:00:00Z")

        notes = automatic_notes(upstream, "GTNH-Questbook-v1.zip")

        self.assertIn("gtnh-questbook-mirror", notes)
        self.assertIn(str(upstream["html_url"]), notes)
        self.assertIn("not GitHub's automatic Source Code ZIP", notes)
        self.assertIn("Extract the downloaded ZIP anywhere", notes)
        self.assertIn("Copy the extracted `betterquesting` folder", notes)
        self.assertIn("`/bq_admin default load`", notes)
        self.assertNotIn("Close GTNH", notes)

    def test_manual_notes_contain_requested_ref_and_resolved_commit(self) -> None:
        notes = manual_notes(
            "feature/quests", "0123456789abcdef", "GTNH-Questbook-preview.zip"
        )

        self.assertIn("`feature/quests`", notes)
        self.assertIn("`0123456789abcdef`", notes)
        self.assertIn("GTNH-Questbook-preview.zip", notes)


class FakeGitHubState:
    def __init__(self) -> None:
        self.authorization: list[tuple[str, str | None]] = []
        self.destination_releases: dict[str, dict[str, object]] = {}
        self.destination_tags: set[str] = set()
        self.upload_status = 201
        self.uploaded = b""
        self.upload_requests = 0
        self.retry_requests = 0
        self.delete_release_requests = 0
        self.delete_tag_requests = 0


def handler_for(state: FakeGitHubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def _record(self) -> None:
            state.authorization.append(
                (self.path, self.headers.get("Authorization"))
            )

        def _json(self, status: int, payload: object, **headers: str) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name.replace("_", "-"), value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._record()
            parsed = urlparse(self.path)
            if parsed.path == "/retry":
                state.retry_requests += 1
                if state.retry_requests == 1:
                    self._json(503, {"message": "temporary"}, Retry_After="0")
                else:
                    self._json(200, {"ok": True})
                return
            if parsed.path == "/repos/upstream/project/releases":
                page = parse_qs(parsed.query).get("page", ["1"])[0]
                if page == "1":
                    next_url = (
                        f"http://{self.headers['Host']}"
                        "/repos/upstream/project/releases?per_page=100&page=2"
                    )
                    self._json(
                        200,
                        [release(1, "one", "2026-09-08T12:00:00Z")],
                        Link=f'<{next_url}>; rel="next"',
                    )
                else:
                    self._json(
                        200, [release(2, "two", "2026-09-09T12:00:00Z")]
                    )
                return
            if parsed.path == "/repos/destination/project/releases":
                self._json(200, list(state.destination_releases.values()))
                return
            release_prefix = "/repos/destination/project/releases/tags/"
            if parsed.path.startswith(release_prefix):
                tag = unquote(parsed.path[len(release_prefix) :])
                found = state.destination_releases.get(tag)
                if found is None:
                    self._json(404, {"message": "Not Found"})
                else:
                    self._json(200, found)
                return
            ref_prefix = "/repos/destination/project/git/ref/tags/"
            if parsed.path.startswith(ref_prefix):
                tag = unquote(parsed.path[len(ref_prefix) :])
                if tag in state.destination_tags:
                    self._json(200, {"ref": f"refs/tags/{tag}"})
                else:
                    self._json(404, {"message": "Not Found"})
                return
            self._json(404, {"message": "Not Found"})

        def do_POST(self) -> None:
            self._record()
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if parsed.path == "/repos/destination/project/releases":
                payload = json.loads(body)
                tag = payload["tag_name"]
                if tag in state.destination_releases or tag in state.destination_tags:
                    self._json(422, {"message": "Validation Failed"})
                    return
                created = dict(payload)
                created.update(
                    {
                        "id": 99,
                        "upload_url": f"http://{self.headers['Host']}/uploads/99{{?name,label}}",
                    }
                )
                state.destination_releases[tag] = created
                state.destination_tags.add(tag)
                self._json(201, created)
                return
            if parsed.path == "/uploads/99":
                state.upload_requests += 1
                if state.upload_status != 201:
                    self._json(state.upload_status, {"message": "upload failed"})
                else:
                    state.uploaded = body
                    name = parse_qs(parsed.query)["name"][0]
                    self._json(201, {"id": 100, "name": name})
                return
            self._json(404, {"message": "Not Found"})

        def do_DELETE(self) -> None:
            self._record()
            parsed = urlparse(self.path)
            if parsed.path == "/repos/destination/project/releases/99":
                state.delete_release_requests += 1
                state.destination_releases.clear()
                self.send_response(204)
                self.end_headers()
                return
            ref_prefix = "/repos/destination/project/git/refs/tags/"
            if parsed.path.startswith(ref_prefix):
                state.delete_tag_requests += 1
                state.destination_tags.discard(unquote(parsed.path[len(ref_prefix) :]))
                self.send_response(204)
                self.end_headers()
                return
            self._json(404, {"message": "Not Found"})

    return Handler


class GitHubClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = FakeGitHubState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = GitHubClient(
            base_url=self.base_url, sleeper=lambda _: None, max_attempts=2
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_paginated_listing_follows_link_and_upstream_is_anonymous(self) -> None:
        releases = self.client.list_releases("upstream/project", token=None)

        self.assertEqual(["one", "two"], [item["tag_name"] for item in releases])
        upstream_headers = [
            auth for path, auth in self.state.authorization if "upstream" in path
        ]
        self.assertEqual([None, None], upstream_headers)

    def test_destination_listing_uses_destination_token(self) -> None:
        self.client.list_releases("destination/project", token="destination-token")

        destination_headers = [
            auth for path, auth in self.state.authorization if "destination" in path
        ]
        self.assertEqual(["Bearer destination-token"], destination_headers)

    def test_transient_server_failure_is_retried(self) -> None:
        payload, _ = self.client.request_json("GET", "/retry")

        self.assertEqual({"ok": True}, payload)
        self.assertEqual(2, self.state.retry_requests)

    def test_automatic_existing_matching_release_is_idempotent(self) -> None:
        upstream = release(30, "v1", "2026-09-09T12:00:00Z")
        self.state.destination_releases["v1"] = {
            "tag_name": "v1",
            "body": marker(30, "2026-09-09T12:00:00Z"),
        }
        self.state.destination_tags.add("v1")

        complete = check_automatic_destination(
            self.client, "destination/project", "destination-token", upstream
        )

        self.assertTrue(complete)

    def test_automatic_tag_owned_by_manual_release_is_rejected(self) -> None:
        upstream = release(30, "v1", "2026-09-09T12:00:00Z")
        self.state.destination_releases["v1"] = {
            "tag_name": "v1",
            "body": "manual release",
        }

        with self.assertRaisesRegex(ReleaseToolError, "not the matching mirror"):
            check_automatic_destination(
                self.client, "destination/project", "destination-token", upstream
            )

    def test_manual_release_collision_is_rejected(self) -> None:
        self.state.destination_releases["preview"] = {
            "tag_name": "preview",
            "body": "existing",
        }

        with self.assertRaisesRegex(ReleaseToolError, "release already exists"):
            ensure_manual_destination_available(
                self.client, "destination/project", "destination-token", "preview"
            )

    def test_manual_tag_only_collision_is_rejected(self) -> None:
        self.state.destination_tags.add("preview")

        with self.assertRaisesRegex(ReleaseToolError, "tag already exists"):
            ensure_manual_destination_available(
                self.client, "destination/project", "destination-token", "preview"
            )

    def test_upload_failure_removes_created_release_and_tag(self) -> None:
        self.state.upload_status = 500
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "questbook.zip"
            archive.write_bytes(b"zip bytes")
            payload = {
                "tag_name": "v1",
                "target_commitish": "abcdef",
                "name": "Release v1",
                "body": "notes",
                "prerelease": False,
            }

            with self.assertRaisesRegex(ReleaseToolError, "upload"):
                publish_release(
                    self.client,
                    "destination/project",
                    "destination-token",
                    payload,
                    archive,
                )

        self.assertEqual({}, self.state.destination_releases)
        self.assertEqual(set(), self.state.destination_tags)
        self.assertEqual(1, self.state.upload_requests)
        self.assertEqual(1, self.state.delete_release_requests)
        self.assertEqual(1, self.state.delete_tag_requests)

    def test_automatic_create_collision_rechecks_matching_release(self) -> None:
        timestamp = "2026-09-09T12:00:00Z"
        self.state.destination_releases["v1"] = {
            "id": 77,
            "tag_name": "v1",
            "body": marker(30, timestamp),
        }
        self.state.destination_tags.add("v1")
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "questbook.zip"
            archive.write_bytes(b"zip bytes")

            existing = publish_release(
                self.client,
                "destination/project",
                "destination-token",
                {
                    "tag_name": "v1",
                    "target_commitish": "abcdef",
                    "name": "Release v1",
                    "body": marker(30, timestamp),
                    "prerelease": False,
                },
                archive,
                idempotent_marker=parse_mirror_marker(marker(30, timestamp)),
            )

        self.assertEqual(77, existing["id"])
        self.assertEqual(0, self.state.upload_requests)

    def test_successful_publication_uploads_exact_archive_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "GTNH-Questbook-v1.zip"
            archive.write_bytes(b"zip bytes")

            created = publish_release(
                self.client,
                "destination/project",
                "destination-token",
                {
                    "tag_name": "v1",
                    "target_commitish": "abcdef",
                    "name": "Release v1",
                    "body": "notes",
                    "prerelease": False,
                },
                archive,
            )

        self.assertEqual(99, created["id"])
        self.assertEqual(b"zip bytes", self.state.uploaded)


class GitValidationTests(unittest.TestCase):
    def test_valid_tag_with_slash_is_accepted(self) -> None:
        validate_git_tag("preview/2026-09-09")

    def test_invalid_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(ReleaseToolError, "not a valid Git tag"):
            validate_git_tag("bad tag")


if __name__ == "__main__":
    unittest.main()
