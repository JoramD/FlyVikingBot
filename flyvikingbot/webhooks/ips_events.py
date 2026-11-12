"""Invision Community 5 webhook events and payload extraction.

Payloads are the IPS REST API objects (GalleryImage, BlogEntry, DownloadsFile, Member),
verified against real captures in tests/fixtures/ips/. A missing optional field just means
less detail in the Discord post; a missing required field returns ``None`` (and is logged).
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


_IMG_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class GalleryImage:
    """A new gallery image; posted to #screenshots."""

    url: str | None  # the image's gallery page
    image_url: str  # the image file itself (large version if available)


@dataclass(frozen=True)
class BlogEntry:
    """A new blog entry; entries from the announcements blog become @everyone announcements."""

    title: str
    url: str
    blog_id: int | None
    image_url: str | None


@dataclass(frozen=True)
class DownloadsFile:
    """A new file (or a new version of one) in the website's downloads."""

    title: str | None
    url: str
    version: str | None


def unwrap(payload: Any) -> dict[str, Any]:
    """Return the content object. IPS 5 sends content bare, and member events as
    {"member": {...}, "changes": {...}}."""
    if not isinstance(payload, dict):
        return {}
    member = payload.get("member")
    return member if isinstance(member, dict) and "url" not in payload else payload


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
    image_url = _first(_nested(data, "images", "large"), _nested(data, "images", "original"))
    if not image_url:
        _missing(GALLERY_IMAGE_CREATE, "images", data)
        return None
    return GalleryImage(url=_str(data.get("url")), image_url=image_url)


def parse_blog_entry(payload: Any) -> BlogEntry | None:
    data = unwrap(payload)
    title, url = _str(data.get("title")), _str(data.get("url"))
    if not title or not url:
        _missing(BLOG_ENTRY_CREATE, "title/url", data)
        return None
    blog = data.get("blog")
    blog_id = _int(blog.get("id")) if isinstance(blog, dict) else _int(blog)
    image_url = _str(data.get("image"))  # cover photo, if set
    body = _str(data.get("entry"))
    if not image_url and body and (match := _IMG_SRC.search(body)):
        image_url = match.group(1)
    return BlogEntry(title=title, url=url, blog_id=blog_id, image_url=image_url)


def parse_downloads_file(payload: Any, event: str = DOWNLOADS_FILE_CREATE) -> DownloadsFile | None:
    data = unwrap(payload)  # downloads_new_version sends the same File object as downloadsFile_create
    url = _str(data.get("url"))
    if not url:
        _missing(event, "url", data)
        return None
    return DownloadsFile(title=_str(data.get("title")), url=url, version=_str(data.get("version")))


# --- members ------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class MemberProfile:
    """The parts of a website member that account linking needs."""

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


# member_edited also fires for bookkeeping, e.g. someone viewing the profile ("pp_last_visitors").
# Events whose `changes` contain only these keys can't affect linking and are skipped. Anything else,
# or a missing/empty `changes`, is processed as usual.
BOOKKEEPING_CHANGES = frozenset(
    {"pp_last_visitors", "members_profile_views", "last_visit", "last_activity", "member_last_post"}
)


def only_bookkeeping_changes(payload: Any) -> bool:
    """True if a member_edited event can't affect linking (only bookkeeping keys changed)."""
    changes = payload.get("changes") if isinstance(payload, dict) else None
    return isinstance(changes, dict) and bool(changes) and set(changes) <= BOOKKEEPING_CHANGES
