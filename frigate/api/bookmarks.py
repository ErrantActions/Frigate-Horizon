"""Shared camera-authorized investigation notes with creator-owned mutations."""

import time
import uuid
from typing import Self

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from frigate.api.auth import (
    allow_any_authenticated,
    get_current_user,
    require_camera_access,
)
from frigate.api.defs.tags import Tags
from frigate.models import RecordingBookmark, Recordings


async def private_bookmarks(response: Response) -> None:
    """Keep permission-dependent notes out of shared HTTP caches."""
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    prefix="/{camera_name}/recordings/bookmarks",
    tags=[Tags.recordings],
    dependencies=[
        Depends(allow_any_authenticated()),
        Depends(require_camera_access),
        Depends(private_bookmarks),
    ],
)


class BookmarkCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    timestamp: float = Field(ge=0, le=253402300799, allow_inf_nan=False)
    end_time: float | None = Field(
        default=None, ge=0, le=253402300799, allow_inf_nan=False
    )
    title: str = Field(min_length=1, max_length=128)
    notes: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def valid_range(self) -> Self:
        """Keep a selected bookmark range positive without inventing coverage."""
        if self.end_time is not None and self.end_time <= self.timestamp:
            raise ValueError("Bookmark end must follow its timestamp")
        return self


class BookmarkEdit(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    title: str = Field(min_length=1, max_length=128)
    notes: str = Field(default="", max_length=4000)
    revision: int = Field(ge=1)


class BookmarkValue(BaseModel):
    id: str
    camera: str
    timestamp: float
    end_time: float | None
    title: str
    notes: str
    owner: str
    created_at: float
    updated_at: float
    revision: int
    can_edit: bool
    available_at_anchor: bool
    protects_footage: bool


class BookmarkPage(BaseModel):
    items: list[BookmarkValue]
    has_more: bool


async def bookmark_user(request: Request) -> dict:
    """Use the authenticated identity, never a caller-supplied owner."""
    user = await get_current_user(request)
    if isinstance(user, JSONResponse):
        raise HTTPException(user.status_code, "Authentication required")
    if not user.get("username"):
        raise HTTPException(401, "Authentication required")
    return user


def bookmark_value(row: RecordingBookmark, user: dict) -> dict:
    """Report database-known anchor coverage without dereferencing archive paths."""
    available = (
        Recordings.select(Recordings.id)
        .where(
            Recordings.camera == row.camera,
            Recordings.start_time <= row.timestamp,
            Recordings.end_time > row.timestamp,
            ~Recordings.delete_requested,
        )
        .exists()
    )
    return {
        "id": row.id,
        "camera": row.camera,
        "timestamp": row.timestamp,
        "end_time": row.end_time,
        "title": row.title,
        "notes": row.notes,
        "owner": row.owner,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "revision": row.revision,
        "can_edit": user["role"] == "admin" or user["username"] == row.owner,
        "available_at_anchor": available,
        "protects_footage": False,
    }


def owned_bookmark(camera: str, identifier: str, user: dict) -> RecordingBookmark:
    """Require both path-camera identity and creator/admin editing permission."""
    row = RecordingBookmark.get_or_none(
        RecordingBookmark.id == identifier,
        RecordingBookmark.camera == camera,
    )
    if row is None:
        raise HTTPException(404, "Bookmark not found")
    if user["role"] != "admin" and user["username"] != row.owner:
        raise HTTPException(
            403, "Only the creator or an administrator can edit this bookmark"
        )
    return row


@router.get("", response_model=BookmarkPage)
def list_bookmarks(
    camera_name: str,
    after: float = Query(0, ge=0, allow_inf_nan=False),
    before: float | None = Query(None, ge=0, allow_inf_nan=False),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: dict = Depends(bookmark_user),
):
    """List shared notes with bounded pagination for one authorized camera."""
    if before is not None and before <= after:
        raise HTTPException(422, "Invalid bookmark query range")
    query = RecordingBookmark.select().where(
        RecordingBookmark.camera == camera_name,
        (RecordingBookmark.timestamp >= after) | (RecordingBookmark.end_time > after),
    )
    if before is not None:
        query = query.where(RecordingBookmark.timestamp < before)
    rows = list(
        query.order_by(RecordingBookmark.timestamp.desc(), RecordingBookmark.id)
        .offset(offset)
        .limit(limit + 1)
    )
    return {
        "items": [bookmark_value(row, user) for row in rows[:limit]],
        "has_more": len(rows) > limit,
    }


@router.post("", status_code=201, response_model=BookmarkValue)
def create_bookmark(
    camera_name: str, body: BookmarkCreate, user: dict = Depends(bookmark_user)
):
    """Save an annotation without changing footage or retention protection."""
    now = time.time()
    row = RecordingBookmark.create(
        id=uuid.uuid4().hex,
        camera=camera_name,
        owner=user["username"],
        timestamp=body.timestamp,
        end_time=body.end_time,
        title=body.title,
        notes=body.notes,
        created_at=now,
        updated_at=now,
    )
    return bookmark_value(row, user)


@router.patch("/{bookmark_id}", response_model=BookmarkValue)
def edit_bookmark(
    camera_name: str,
    bookmark_id: str,
    body: BookmarkEdit,
    user: dict = Depends(bookmark_user),
):
    """Compare revisions so concurrent edits cannot silently overwrite notes."""
    owned_bookmark(camera_name, bookmark_id, user)
    changed = (
        RecordingBookmark.update(
            title=body.title,
            notes=body.notes,
            updated_at=time.time(),
            revision=RecordingBookmark.revision + 1,
        )
        .where(
            RecordingBookmark.id == bookmark_id,
            RecordingBookmark.camera == camera_name,
            RecordingBookmark.revision == body.revision,
        )
        .execute()
    )
    if not changed:
        raise HTTPException(
            409, "Bookmark changed or was deleted; refresh before editing"
        )
    row = RecordingBookmark.get_or_none(RecordingBookmark.id == bookmark_id)
    if row is None:
        raise HTTPException(404, "Bookmark was deleted")
    return bookmark_value(row, user)


@router.delete("/{bookmark_id}", status_code=204)
def delete_bookmark(
    camera_name: str,
    bookmark_id: str,
    revision: int = Query(..., ge=1),
    user: dict = Depends(bookmark_user),
):
    """Delete only the annotation, guarding against stale destructive actions."""
    owned_bookmark(camera_name, bookmark_id, user)
    deleted = (
        RecordingBookmark.delete()
        .where(
            RecordingBookmark.id == bookmark_id,
            RecordingBookmark.camera == camera_name,
            RecordingBookmark.revision == revision,
        )
        .execute()
    )
    if not deleted:
        raise HTTPException(
            409, "Bookmark changed or was deleted; refresh before deleting"
        )
