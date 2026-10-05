#!/usr/bin/env python3
"""Build and publish questbook-only releases from public GTNH sources."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


SOURCE_QUESTBOOK_PREFIX = "config/betterquesting/"
ARCHIVE_QUESTBOOK_PREFIX = "betterquesting/"
MARKER_PREFIX = "gtnh-questbook-mirror:"
MARKER_RE = re.compile(
    r"<!--\s*gtnh-questbook-mirror:(.*?)\s*-->", re.DOTALL
)


class ReleaseToolError(RuntimeError):
    """A safe, user-facing release operation failure."""


class GitHubAPIError(ReleaseToolError):
    """A GitHub API response outside the accepted status set."""

    def __init__(self, status: int, message: str, url: str) -> None:
        super().__init__(f"GitHub API returned HTTP {status} for {url}: {message}")
        self.status = status


def _timestamp(value: object, *, context: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ReleaseToolError(f"{context} has no valid publication timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseToolError(
            f"{context} has invalid publication timestamp {value!r}"
        ) from error
    if parsed.tzinfo is None:
        raise ReleaseToolError(f"{context} publication timestamp lacks a timezone")
    return parsed


def _release_key(release: Mapping[str, Any]) -> tuple[datetime, int]:
    release_id = release.get("id")
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        raise ReleaseToolError("upstream release has no valid numeric ID")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ReleaseToolError(f"upstream release {release_id} has no valid tag")
    return _timestamp(release.get("published_at"), context=f"release {tag!r}"), release_id


def parse_mirror_marker(body: str | None) -> tuple[datetime, int] | None:
    """Return a validated (published_at, release_id) watermark from notes."""
    if not body or MARKER_PREFIX not in body:
        return None
    match = MARKER_RE.search(body)
    if match is None:
        raise ReleaseToolError("destination release has a malformed mirror marker")
    try:
        payload = json.loads(match.group(1))
        release_id = payload["upstream_release_id"]
        published_at = payload["published_at"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ReleaseToolError("destination release has a malformed mirror marker") from error
    if not isinstance(release_id, int) or isinstance(release_id, bool):
        raise ReleaseToolError("destination release has a malformed mirror marker ID")
    return _timestamp(published_at, context="destination mirror marker"), release_id


def select_releases_to_sync(
    upstream: Sequence[Mapping[str, Any]],
    downstream: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Select first-run latest or all releases after the newest watermark."""
    keyed_upstream = [(_release_key(item), item) for item in upstream]
    if not keyed_upstream:
        return []

    watermarks: list[tuple[datetime, int]] = []
    for destination_release in downstream:
        marker = parse_mirror_marker(destination_release.get("body"))
        if marker is not None:
            watermarks.append(marker)

    if not watermarks:
        return [max(keyed_upstream, key=lambda pair: pair[0])[1]]

    newest_watermark = max(watermarks)
    selected = [pair for pair in keyed_upstream if pair[0] > newest_watermark]
    return [item for _, item in sorted(selected, key=lambda pair: pair[0])]


def select_asset(release: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select an attached modpack ZIP without considering source archives."""
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ReleaseToolError("upstream release has no valid tag")
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list):
        raise ReleaseToolError(f"upstream release {tag!r} has invalid asset metadata")
    zip_assets = [
        item
        for item in raw_assets
        if isinstance(item, Mapping)
        and isinstance(item.get("name"), str)
        and item["name"].lower().endswith(".zip")
    ]
    exact = [item for item in zip_assets if item["name"] == f"{tag}.zip"]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ReleaseToolError(
            f"upstream release {tag!r} has duplicate attached ZIP assets named {tag}.zip"
        )
    if len(zip_assets) == 1:
        return zip_assets[0]
    if not zip_assets:
        raise ReleaseToolError(f"upstream release {tag!r} has no attached ZIP asset")
    names = ", ".join(repr(item["name"]) for item in zip_assets)
    raise ReleaseToolError(
        f"upstream release {tag!r} has ambiguous attached ZIP assets: {names}"
    )


def sanitize_tag(tag: str) -> str:
    """Convert a Git tag into a safe release-asset filename component."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", tag).strip("._-")
    return (safe or "release")[:180]


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.orig_filename
    if "\\" in name:
        raise ReleaseToolError(f"ZIP member contains a backslash: {name!r}")
    path = PurePosixPath(name)
    if (
        not name
        or "\x00" in name
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:/", name)
    ):
        raise ReleaseToolError(f"unsafe ZIP member path: {name!r}")
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise ReleaseToolError(f"ZIP member is a symbolic link: {name!r}")


def _write_zip_from_archive(source: Path, destination: zipfile.ZipFile) -> int:
    seen: set[str] = set()
    selected: list[tuple[str, zipfile.ZipInfo]] = []
    with zipfile.ZipFile(source) as source_archive:
        for info in source_archive.infolist():
            _validate_zip_member(info)
            name = info.filename
            if info.is_dir() or not name.startswith(SOURCE_QUESTBOOK_PREFIX):
                continue
            output_name = ARCHIVE_QUESTBOOK_PREFIX + name.removeprefix(
                SOURCE_QUESTBOOK_PREFIX
            )
            if output_name in seen:
                raise ReleaseToolError(
                    f"duplicate questbook ZIP member: {output_name!r}"
                )
            seen.add(output_name)
            selected.append((output_name, info))

        for name, info in sorted(selected, key=lambda item: item[0]):
            output_info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            output_info.compress_type = zipfile.ZIP_DEFLATED
            output_info.external_attr = (stat.S_IFREG | 0o644) << 16
            with source_archive.open(info) as source_file:
                with destination.open(output_info, "w") as output_file:
                    shutil.copyfileobj(source_file, output_file, length=1024 * 1024)
    return len(selected)


def _write_zip_from_directory(source: Path, destination: zipfile.ZipFile) -> int:
    questbook = source / "config" / "betterquesting"
    if not questbook.is_dir():
        raise ReleaseToolError(
            f"source has no directory at {SOURCE_QUESTBOOK_PREFIX.rstrip('/')}"
        )
    selected: list[tuple[str, Path]] = []
    for path in questbook.rglob("*"):
        if path.is_symlink():
            raise ReleaseToolError(f"questbook source contains a symbolic link: {path}")
        if path.is_file():
            selected.append(
                (
                    ARCHIVE_QUESTBOOK_PREFIX
                    + path.relative_to(questbook).as_posix(),
                    path,
                )
            )
    for name, path in sorted(selected, key=lambda item: item[0]):
        output_info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        output_info.compress_type = zipfile.ZIP_DEFLATED
        output_info.external_attr = (stat.S_IFREG | 0o644) << 16
        with path.open("rb") as source_file:
            with destination.open(output_info, "w") as output_file:
                shutil.copyfileobj(source_file, output_file, length=1024 * 1024)
    return len(selected)


def build_questbook_zip(source: Path, output: Path) -> int:
    """Create and validate a questbook-only ZIP from a modpack ZIP or checkout."""
    source = Path(source)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    completed_ok = False
    try:
        with zipfile.ZipFile(
            output, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as destination:
            if source.is_dir():
                count = _write_zip_from_directory(source, destination)
            elif source.is_file():
                count = _write_zip_from_archive(source, destination)
            else:
                raise ReleaseToolError(f"source path does not exist: {source}")
        if count == 0:
            raise ReleaseToolError(
                "source contains no files beneath "
                f"{SOURCE_QUESTBOOK_PREFIX.rstrip('/')}"
            )
        with zipfile.ZipFile(output) as completed:
            names = completed.namelist()
            if len(names) != count or any(
                not name.startswith(ARCHIVE_QUESTBOOK_PREFIX) for name in names
            ):
                raise ReleaseToolError("completed archive failed questbook layout validation")
            bad = completed.testzip()
            if bad is not None:
                raise ReleaseToolError(f"completed archive contains corrupt member {bad!r}")
        completed_ok = True
        return count
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ReleaseToolError(f"could not build questbook archive: {error}") from error
    finally:
        if not completed_ok:
            output.unlink(missing_ok=True)


def _installation_text(asset_name: str) -> str:
    return f"""Download the attached `{asset_name}`, not GitHub's automatic Source Code ZIP.

## Installation

1. Extract the downloaded ZIP anywhere.
2. Delete the existing `<GTNH instance>/config/betterquesting` folder.
3. Copy the extracted `betterquesting` folder into `<GTNH instance>/config`.
4. In-game, run `/bq_admin default load` to load the new questbook.

Deleting the old folder first prevents files removed by a newer questbook from remaining behind.
"""


def automatic_notes(release: Mapping[str, Any], asset_name: str) -> str:
    tag = str(release["tag_name"])
    marker = json.dumps(
        {
            "published_at": release["published_at"],
            "upstream_release_id": release["id"],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"""This is a questbook-only extraction of the corresponding GT New Horizons release [{tag}]({release['html_url']}).

{_installation_text(asset_name)}
<!-- gtnh-questbook-mirror:{marker} -->"""


def manual_notes(source_ref: str, commit: str, asset_name: str) -> str:
    return f"""This questbook-only preview was generated from upstream ref `{source_ref}` at commit `{commit}` in `GTNewHorizons/GT-New-Horizons-Modpack`.

{_installation_text(asset_name)}"""


def _api_message(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "request failed"
    message = payload.get("message") if isinstance(payload, dict) else None
    return message if isinstance(message, str) else "request failed"


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for item in header.split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="([^"]+)"', item)
        if match and match.group(2) == "next":
            return match.group(1)
    return None


class GitHubClient:
    """Small GitHub REST client with explicit per-request authentication."""

    def __init__(
        self,
        base_url: str = "https://api.github.com",
        *,
        sleeper: Any = time.sleep,
        max_attempts: int = 3,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.sleeper = sleeper
        self.max_attempts = max_attempts
        self.timeout = timeout

    def _url(self, path_or_url: str) -> str:
        if urlparse(path_or_url).scheme:
            return path_or_url
        return urljoin(f"{self.base_url}/", path_or_url.lstrip("/"))

    def request_bytes(
        self,
        method: str,
        path_or_url: str,
        *,
        token: str | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[bytes, Message]:
        url = self._url(path_or_url)
        last_error: BaseException | None = None
        for attempt in range(1, self.max_attempts + 1):
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "GTNH-Questbook-Releases",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            if content_type is not None:
                headers["Content-Type"] = content_type
            request = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    if response.status not in expected:
                        raise GitHubAPIError(
                            response.status, _api_message(body), url
                        )
                    return body, response.headers
            except HTTPError as error:
                body = error.read(4096)
                if error.code in expected:
                    return body, error.headers
                last_error = GitHubAPIError(
                    error.code, _api_message(body), url
                )
                retryable = method in {"GET", "HEAD", "DELETE"} and (
                    error.code == 429 or 500 <= error.code <= 599
                )
                if not retryable or attempt == self.max_attempts:
                    raise last_error from error
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else float(2 ** (attempt - 1))
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
                if method not in {"GET", "HEAD", "DELETE"} or attempt == self.max_attempts:
                    raise ReleaseToolError(f"GitHub request failed for {url}") from error
                delay = float(2 ** (attempt - 1))
            self.sleeper(min(delay, 30.0))
        raise ReleaseToolError(f"GitHub request failed for {url}") from last_error

    def request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        token: str | None = None,
        payload: Mapping[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> tuple[Any, Message]:
        data = None
        content_type = None
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
            content_type = "application/json"
        body, headers = self.request_bytes(
            method,
            path_or_url,
            token=token,
            data=data,
            content_type=content_type,
            expected=expected,
        )
        if not body:
            return None, headers
        try:
            return json.loads(body), headers
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ReleaseToolError(
                f"GitHub returned invalid JSON for {self._url(path_or_url)}"
            ) from error

    def list_releases(
        self, repository: str, *, token: str | None
    ) -> list[Mapping[str, Any]]:
        path: str | None = f"/repos/{repository}/releases?per_page=100"
        releases: list[Mapping[str, Any]] = []
        base = urlparse(self.base_url)
        while path is not None:
            payload, headers = self.request_json("GET", path, token=token)
            if not isinstance(payload, list) or any(
                not isinstance(item, Mapping) for item in payload
            ):
                raise ReleaseToolError(
                    f"GitHub returned invalid release data for {repository}"
                )
            releases.extend(payload)
            path = _next_link(headers.get("Link"))
            if path is not None:
                parsed = urlparse(path)
                if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
                    raise ReleaseToolError("GitHub pagination pointed to another origin")
        return releases

    def get_release_by_tag(
        self, repository: str, tag: str, *, token: str
    ) -> Mapping[str, Any] | None:
        path = f"/repos/{repository}/releases/tags/{quote(tag, safe='')}"
        try:
            payload, _ = self.request_json("GET", path, token=token)
        except GitHubAPIError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(payload, Mapping):
            raise ReleaseToolError("GitHub returned invalid destination release data")
        return payload

    def get_tag_ref(
        self, repository: str, tag: str, *, token: str
    ) -> Mapping[str, Any] | None:
        path = f"/repos/{repository}/git/ref/tags/{quote(tag, safe='')}"
        try:
            payload, _ = self.request_json("GET", path, token=token)
        except GitHubAPIError as error:
            if error.status == 404:
                return None
            raise
        if not isinstance(payload, Mapping):
            raise ReleaseToolError("GitHub returned invalid destination tag data")
        return payload

    def create_release(
        self, repository: str, token: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        created, _ = self.request_json(
            "POST",
            f"/repos/{repository}/releases",
            token=token,
            payload=payload,
            expected=(201,),
        )
        if not isinstance(created, Mapping):
            raise ReleaseToolError("GitHub returned invalid created release data")
        return created

    def upload_asset(
        self, upload_url: str, token: str, archive: Path
    ) -> Mapping[str, Any]:
        base_url = upload_url.split("{", 1)[0]
        separator = "&" if "?" in base_url else "?"
        target = f"{base_url}{separator}{urlencode({'name': archive.name})}"
        body, _ = self.request_bytes(
            "POST",
            target,
            token=token,
            data=archive.read_bytes(),
            content_type="application/zip",
            expected=(201,),
        )
        try:
            uploaded = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ReleaseToolError("GitHub returned invalid uploaded asset data") from error
        if not isinstance(uploaded, Mapping):
            raise ReleaseToolError("GitHub returned invalid uploaded asset data")
        return uploaded

    def delete_release(self, repository: str, release_id: int, *, token: str) -> None:
        self.request_bytes(
            "DELETE",
            f"/repos/{repository}/releases/{release_id}",
            token=token,
            expected=(204,),
        )

    def delete_tag(self, repository: str, tag: str, *, token: str) -> None:
        try:
            self.request_bytes(
                "DELETE",
                f"/repos/{repository}/git/refs/tags/{quote(tag, safe='')}",
                token=token,
                expected=(204,),
            )
        except GitHubAPIError as error:
            if error.status != 404:
                raise


def validate_git_tag(tag: str) -> None:
    if not tag or "\r" in tag or "\n" in tag:
        raise ReleaseToolError(f"{tag!r} is not a valid Git tag")
    result = subprocess.run(
        ["git", "check-ref-format", f"refs/tags/{tag}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseToolError(f"{tag!r} is not a valid Git tag")


def check_automatic_destination(
    client: GitHubClient,
    repository: str,
    token: str,
    upstream_release: Mapping[str, Any],
) -> bool:
    tag = str(upstream_release["tag_name"])
    existing = client.get_release_by_tag(repository, tag, token=token)
    if existing is not None:
        existing_marker = parse_mirror_marker(existing.get("body"))
        if existing_marker == _release_key(upstream_release):
            return True
        raise ReleaseToolError(
            f"destination release {tag!r} exists but is not the matching mirror"
        )
    if client.get_tag_ref(repository, tag, token=token) is not None:
        raise ReleaseToolError(
            f"destination tag {tag!r} already exists without a mirrored release"
        )
    return False


def ensure_manual_destination_available(
    client: GitHubClient, repository: str, token: str, tag: str
) -> None:
    if client.get_release_by_tag(repository, tag, token=token) is not None:
        raise ReleaseToolError(f"destination release already exists for tag {tag!r}")
    if client.get_tag_ref(repository, tag, token=token) is not None:
        raise ReleaseToolError(f"destination tag already exists: {tag!r}")


def publish_release(
    client: GitHubClient,
    repository: str,
    token: str,
    payload: Mapping[str, Any],
    archive: Path,
    *,
    idempotent_marker: tuple[datetime, int] | None = None,
) -> Mapping[str, Any]:
    try:
        created = client.create_release(repository, token, payload)
    except GitHubAPIError as error:
        tag = payload.get("tag_name")
        if error.status != 422 or idempotent_marker is None or not isinstance(tag, str):
            raise
        existing = client.get_release_by_tag(repository, tag, token=token)
        if (
            existing is not None
            and parse_mirror_marker(existing.get("body")) == idempotent_marker
        ):
            return existing
        if client.get_tag_ref(repository, tag, token=token) is not None:
            raise ReleaseToolError(
                f"destination tag {tag!r} collided during release creation"
            ) from error
        raise
    release_id = created.get("id")
    upload_url = created.get("upload_url")
    tag = payload.get("tag_name")
    if not isinstance(release_id, int) or not isinstance(upload_url, str):
        raise ReleaseToolError("created release lacks an ID or asset upload URL")
    try:
        client.upload_asset(upload_url, token, archive)
    except ReleaseToolError as error:
        cleanup_errors: list[str] = []
        try:
            client.delete_release(repository, release_id, token=token)
        except ReleaseToolError as cleanup_error:
            cleanup_errors.append(f"release cleanup failed: {cleanup_error}")
        if isinstance(tag, str):
            try:
                client.delete_tag(repository, tag, token=token)
            except ReleaseToolError as cleanup_error:
                cleanup_errors.append(f"tag cleanup failed: {cleanup_error}")
        suffix = f"; {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise ReleaseToolError(f"release asset upload failed{suffix}") from error
    return created


def download_file(
    url: str,
    target: Path,
    *,
    sleeper: Any = time.sleep,
    max_attempts: int = 3,
    timeout: int = 60,
) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }:
        raise ReleaseToolError(f"refusing unexpected release asset URL origin: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(f"{target.suffix}.part")
    for attempt in range(1, max_attempts + 1):
        try:
            request = Request(
                url,
                headers={
                    "Accept": "application/octet-stream",
                    "User-Agent": "GTNH-Questbook-Releases",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            partial.replace(target)
            return
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            partial.unlink(missing_ok=True)
            retryable = not isinstance(error, HTTPError) or (
                error.code == 429 or 500 <= error.code <= 599
            )
            if not retryable or attempt == max_attempts:
                raise ReleaseToolError(f"release asset download failed for {url}") from error
            sleeper(min(float(2 ** (attempt - 1)), 30.0))


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ReleaseToolError(f"required environment variable {name} is missing")
    return value


def _destination_environment() -> tuple[str, str, str]:
    repository = _required_environment("GITHUB_REPOSITORY")
    token = _required_environment("GITHUB_TOKEN")
    target = _required_environment("GITHUB_SHA")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseToolError("GITHUB_REPOSITORY is invalid")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", target):
        raise ReleaseToolError("GITHUB_SHA is not a full commit SHA")
    return repository, token, target


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True, sort_keys=True))


def sync_command(args: argparse.Namespace) -> None:
    repository, token, target_commit = _destination_environment()
    client = GitHubClient()
    upstream_repository = "GTNewHorizons/GT-New-Horizons-Modpack"
    upstream = client.list_releases(upstream_repository, token=None)
    downstream = client.list_releases(repository, token=token)
    selected = select_releases_to_sync(upstream, downstream)
    if not selected:
        _log("sync_complete", releases_published=0)
        return
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    for upstream_release in selected:
        tag = str(upstream_release["tag_name"])
        if check_automatic_destination(
            client, repository, token, upstream_release
        ):
            _log("release_already_mirrored", tag=tag)
            continue
        upstream_asset = select_asset(upstream_release)
        asset_id = upstream_asset.get("id")
        asset_name = upstream_asset.get("name")
        asset_size = upstream_asset.get("size")
        download_url = upstream_asset.get("browser_download_url")
        if (
            not isinstance(asset_id, int)
            or not isinstance(asset_name, str)
            or not isinstance(asset_size, int)
            or not isinstance(download_url, str)
        ):
            raise ReleaseToolError(f"upstream release {tag!r} has invalid asset data")
        _log(
            "selected_upstream_asset",
            tag=tag,
            release_id=upstream_release["id"],
            asset_id=asset_id,
            asset_name=asset_name,
            asset_size=asset_size,
        )
        release_dir = work_dir / str(upstream_release["id"])
        release_dir.mkdir(parents=True, exist_ok=True)
        source_archive = release_dir / "upstream-modpack.zip"
        output_name = f"GTNH-Questbook-{sanitize_tag(tag)}.zip"
        output_archive = release_dir / output_name
        download_file(download_url, source_archive)
        file_count = build_questbook_zip(source_archive, output_archive)
        release_name = upstream_release.get("name")
        if not isinstance(release_name, str) or not release_name.strip():
            release_name = tag
        payload = {
            "tag_name": tag,
            "target_commitish": target_commit,
            "name": release_name,
            "body": automatic_notes(upstream_release, output_name),
            "prerelease": bool(upstream_release.get("prerelease", False)),
        }
        publish_release(
            client,
            repository,
            token,
            payload,
            output_archive,
            idempotent_marker=_release_key(upstream_release),
        )
        _log("release_published", tag=tag, asset=output_name, files=file_count)


def package_command(args: argparse.Namespace) -> None:
    validate_git_tag(args.release_tag)
    output = Path(args.output_dir) / (
        f"GTNH-Questbook-{sanitize_tag(args.release_tag)}.zip"
    )
    count = build_questbook_zip(Path(args.source), output)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output_file:
            output_file.write(f"archive={output}\n")
    _log("archive_created", archive=str(output), files=count)


def publish_manual_command(args: argparse.Namespace) -> None:
    repository, token, target_commit = _destination_environment()
    validate_git_tag(args.release_tag)
    ensure_manual_destination_available(
        GitHubClient(), repository, token, args.release_tag
    )
    archive = Path(args.archive)
    if not archive.is_file():
        raise ReleaseToolError(f"manual release archive does not exist: {archive}")
    release_name = args.release_name.strip() or args.release_tag
    payload = {
        "tag_name": args.release_tag,
        "target_commitish": target_commit,
        "name": release_name,
        "body": manual_notes(args.source_ref, args.source_commit, archive.name),
        "prerelease": True,
    }
    publish_release(GitHubClient(), repository, token, payload, archive)
    _log("manual_release_published", tag=args.release_tag, asset=archive.name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="mirror new upstream releases")
    sync.add_argument("--work-dir", required=True)
    sync.set_defaults(handler=sync_command)

    package = commands.add_parser("package", help="package a checked-out questbook")
    package.add_argument("--source", required=True)
    package.add_argument("--release-tag", required=True)
    package.add_argument("--output-dir", required=True)
    package.set_defaults(handler=package_command)

    manual = commands.add_parser("publish-manual", help="publish a manual release")
    manual.add_argument("--archive", required=True)
    manual.add_argument("--release-tag", required=True)
    manual.add_argument("--release-name", default="")
    manual.add_argument("--source-ref", required=True)
    manual.add_argument("--source-commit", required=True)
    manual.set_defaults(handler=publish_manual_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        args.handler(args)
    except ReleaseToolError as error:
        print(f"error: {json.dumps(str(error), ensure_ascii=True)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
