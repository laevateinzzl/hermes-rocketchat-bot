"""Tests for attachment normalization, classification, and download."""

import pytest

from adapter import (
    AttachmentCandidate,
    attachment_candidates_from_message,
    classify_attachment,
    resolve_message_media,
    sanitize_filename,
)


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------


def test_classify_image_by_mime():
    candidate = AttachmentCandidate(
        url="https://chat.example.com/file-upload/abc/photo.jpg",
        mime_type="image/png",
        title="photo.jpg",
    )
    assert classify_attachment(candidate) == "image"


def test_classify_video_by_mime():
    candidate = AttachmentCandidate(
        url="https://chat.example.com/file-upload/abc/video.mp4",
        mime_type="video/mp4",
        title="video.mp4",
    )
    assert classify_attachment(candidate) == "video"


def test_classify_audio_by_mime():
    candidate = AttachmentCandidate(
        url="https://chat.example.com/file-upload/abc/note.ogg",
        mime_type="audio/ogg",
        title="note.ogg",
    )
    assert classify_attachment(candidate) == "audio"


def test_classify_document_by_extension_fallback():
    candidate = AttachmentCandidate(
        url="https://chat.example.com/file-upload/abc/report.pdf",
        mime_type="",
        title="report.pdf",
    )
    assert classify_attachment(candidate) == "document"


def test_classify_unknown_returns_document():
    candidate = AttachmentCandidate(
        url="https://chat.example.com/file-upload/abc/thing.xyz",
        mime_type="",
        title="thing.xyz",
    )
    assert classify_attachment(candidate) == "document"


def test_classify_audio_as_voice_when_ogg():
    candidate = AttachmentCandidate(
        url="https://chat.example.com/file-upload/abc/voice.ogg",
        mime_type="audio/ogg",
        title="voice.ogg",
    )
    assert classify_attachment(candidate) == "audio"


# ---------------------------------------------------------------------------
# Candidate extraction tests
# ---------------------------------------------------------------------------


def test_normalize_attachments_field():
    message = {
        "_id": "msg1",
        "attachments": [
            {"title": "photo.jpg", "image_url": "http://rc/file-upload/1.jpg"},
            {"title": "doc.pdf", "title_link": "http://rc/file-upload/2.pdf"},
        ],
    }

    candidates = attachment_candidates_from_message(message)

    assert len(candidates) == 2
    assert candidates[0].url == "http://rc/file-upload/1.jpg"
    assert candidates[0].title == "photo.jpg"
    assert candidates[1].url == "http://rc/file-upload/2.pdf"
    assert candidates[1].title == "doc.pdf"


def test_normalize_file_field():
    message = {
        "_id": "msg1",
        "file": {
            "_id": "file1",
            "name": "screenshot.png",
            "type": "image/png",
        },
    }

    candidates = attachment_candidates_from_message(message)

    assert len(candidates) == 1
    assert candidates[0].title == "screenshot.png"
    assert candidates[0].mime_type == "image/png"
    assert candidates[0].rc_file_id == "file1"


def test_normalize_files_field():
    message = {
        "_id": "msg1",
        "files": [
            {"_id": "f1", "name": "img1.png", "type": "image/png"},
            {"_id": "f2", "name": "img2.jpg", "type": "image/jpeg"},
        ],
    }

    candidates = attachment_candidates_from_message(message)

    assert len(candidates) == 2
    assert candidates[0].rc_file_id == "f1"
    assert candidates[1].rc_file_id == "f2"


def test_empty_message_no_candidates():
    candidates = attachment_candidates_from_message({"_id": "msg1"})
    assert candidates == []


# ---------------------------------------------------------------------------
# Filename sanitization tests
# ---------------------------------------------------------------------------


def test_sanitize_filename_keeps_safe_name():
    assert sanitize_filename("photo.jpg") == "photo.jpg"


def test_sanitize_filename_replaces_path_separators():
    assert sanitize_filename("../../etc/passwd") == "_.._etc_passwd"


def test_sanitize_filename_strips_null_and_control_chars():
    result = sanitize_filename("test\x00file.txt")
    assert "\x00" not in result


# ---------------------------------------------------------------------------
# resolve_message_media tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_media_returns_local_paths():
    """Public URLs are passed through; protected ones get downloaded."""
    message = {
        "_id": "msg1",
        "attachments": [
            {"title": "photo.jpg", "image_url": "http://rc/file-upload/photo.jpg"},
        ],
    }

    # Mock a client that returns dummy data when download is called
    class FakeDownloadClient:
        server_url = "http://rc"
        _user_id = "bot1"
        _access_token = "tok"

        async def download_attachment(self, url):
            return b"fake-image-data"

    client = FakeDownloadClient()

    media_urls, media_types, media_text_inlined = await resolve_message_media(
        message, client
    )

    assert len(media_urls) == 1
    assert len(media_types) == 1
    assert media_types[0] == "image"
    # Non-text attachments carry no inlining contract.
    assert media_text_inlined == [None]


@pytest.mark.asyncio
async def test_resolve_media_text_attachment_not_inlined():
    """text/* attachments are cached but NOT inlined into event.text.

    Hermes' ``media_text_inlined`` contract (00394acfae): False tells the
    gateway the content lives in the cached file and the agent must read
    it itself.
    """
    message = {
        "_id": "msg1",
        "files": [
            {
                "_id": "f1",
                "name": "notes.txt",
                "type": "text/plain",
            },
        ],
        "rid": "room-1",
    }

    class FakeDownloadClient:
        server_url = "http://rc"
        _user_id = "bot1"
        _access_token = "tok"

        async def download_attachment(self, url):
            return b"hello world"

    media_urls, media_types, media_text_inlined = await resolve_message_media(
        message, FakeDownloadClient()
    )

    assert len(media_urls) == 1
    assert media_types == ["document"]
    # Relative /file-upload/... paths are absolutized against the server.
    assert media_urls == ["http://rc/file-upload/room-1/f1/notes.txt"]
    assert media_text_inlined == [False]


@pytest.mark.asyncio
async def test_resolve_media_no_attachments_returns_empty():
    message = {"_id": "msg1"}
    client = object()  # not used

    media_urls, media_types, media_text_inlined = await resolve_message_media(
        message, client
    )

    assert media_urls == []
    assert media_types == []
    assert media_text_inlined == []
