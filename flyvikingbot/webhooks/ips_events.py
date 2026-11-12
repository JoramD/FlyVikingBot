"""Invision Community 5 webhook events and payload extraction.

The payloads are expected to follow the IPS REST API object shapes (GalleryImage,
BlogEntry, DownloadsFile). Until real samples are captured, every extractor is
defensive: a missing optional field just means less detail in the Discord post,
and a missing required field returns ``None`` (and gets logged) instead of crashing.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

GALLERY_IMAGE_CREATE = "galleryImage_create"
BLOG_ENTRY_CREATE = "blogEntry_create"
DOWNLOADS_FILE_CREATE = "downloadsFile_create"
DOWNLOADS_NEW_VERSION = "downloads_new_version"

MEMBER_EDITED = "member_edited"
MEMBER_REGISTRATION_COMPLETE = "member_registration_complete"
MEMBER_EVENTS = frozenset({MEMBER_EDITED, MEMBER_REGISTRATION_COMPLETE})

KNOWN_EVENTS = frozenset(
    {GALLERY_IMAGE_CREATE, BLOG_ENTRY_CREATE, DOWNLOADS_FILE_CREATE, DOWNLOADS_NEW_VERSION} | MEMBER_EVENTS
)

_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class GalleryImage:
    title: str | None
    url: str | None
    image_url: str


@dataclass(frozen=True)
class BlogEntry:
    title: str
    url: str
    blog_id: int | None
    image_url: str | None


@dataclass(frozen=True)
class DownloadsFile:
    title: str | None
    url: str
    version: str | None


def unwrap(payload: Any) -> dict[str, Any]:
    """Return the content object, whether IPS sends it bare or wrapped in an envelope."""
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    if not isinstance(payload, dict):
        return {}
    for key in ("data", "item", "content", "payload"):
        inner = payload.get(key)
        if "url" not in payload and isinstance(inner, dict):
            return inner
    return payload


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _first(*values: Any) -> str | None:
    for value in values:
        if s := _str(value):
            return s
    return None


def _nested(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _missing(event: str, field: str, data: dict[str, Any]) -> None:
    log.warning("%s payload has no usable %r (keys: %s)", event, field, sorted(data))


def parse_gallery_image(payload: Any) -> GalleryImage | None:
    data = unwrap(payload)
    image_url = _first(
        _nested(data, "images", "large"),
        _nested(data, "images", "original"),
        _nested(data, "images", "medium"),
        data.get("image"),
    )
    if not image_url:
        _missing(GALLERY_IMAGE_CREATE, "images", data)
        return None
    return GalleryImage(title=_str(data.get("title")), url=_str(data.get("url")), image_url=image_url)


def parse_blog_entry(payload: Any) -> BlogEntry | None:
    data = unwrap(payload)
    title, url = _str(data.get("title")), _str(data.get("url"))
    if not title or not url:
        _missing(BLOG_ENTRY_CREATE, "title/url", data)
        return None
    blog = data.get("blog")
    blog_id = _int(blog.get("id")) if isinstance(blog, dict) else _int(blog)
    image_url = _first(data.get("image"), data.get("coverPhoto"), _nested(data, "coverPhoto", "url"))
    body = _str(data.get("entry") or data.get("content"))
    if not image_url and body and (match := _IMG_SRC.search(body)):
        image_url = match.group(1)
    return BlogEntry(title=title, url=url, blog_id=blog_id, image_url=image_url)


def parse_downloads_file(payload: Any, event: str = DOWNLOADS_FILE_CREATE) -> DownloadsFile | None:
    data = unwrap(payload)
    # A "new version" event may carry the file nested in the version object
    if not _str(data.get("url")) and isinstance(data.get("file"), dict):
        data = data["file"]
    url = _str(data.get("url"))
    if not url:
        _missing(event, "url", data)
        return None
    return DownloadsFile(title=_str(data.get("title")), url=url, version=_str(data.get("version")))


# --- members ------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MemberProfile:
    member_id: int
    name: str | None
    profile_url: str | None
    fsa_pilot_id: str | None
    discord_name: str | None


def _custom_field(data: dict[str, Any], field_id: int | None) -> str | None:
    """Find a custom profile field by ID. IPS nests them as
    {"<group id>": {"name": ..., "fields": {"<field id>": {"name": ..., "value": ...}}}}."""
    if field_id is None:
        return None
    groups = data.get("customFields")
    if not isinstance(groups, dict):
        return None
    for group in groups.values():
        fields = group.get("fields") if isinstance(group, dict) else None
        if isinstance(fields, dict) and isinstance(field := fields.get(str(field_id)), dict):
            value = field.get("value")
            if value is None:
                return None
            text = html.unescape(_TAG.sub(" ", str(value)))
            return " ".join(text.split()) or None
    return None


def parse_member_profile(
    payload: Any, fsa_field_id: int | None, discord_field_id: int | None
) -> MemberProfile | None:
    data = unwrap(payload)
    member_id = _int(data.get("id"))
    if member_id is None:
        _missing(MEMBER_EDITED, "id", data)
        return None
    return MemberProfile(
        member_id=member_id,
        name=_str(data.get("name")),
        profile_url=_str(data.get("profileUrl")),
        fsa_pilot_id=_custom_field(data, fsa_field_id),
        discord_name=_custom_field(data, discord_field_id),
    )
