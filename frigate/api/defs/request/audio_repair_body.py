"""User-controlled recording audio maintenance requests."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AudioRepairScanBody(BaseModel):
    """Bound an indexed, read-only scan; no caller-controlled filesystem paths."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cameras: list[str] | None = Field(default=None, max_length=100)
    start: float | None = None
    end: float | None = None
    limit: int = Field(default=1000, ge=1, le=10000)

    @model_validator(mode="after")
    def validate_window(self):
        """Reject inverted intervals and empty camera selectors."""
        if self.start is not None and self.end is not None and self.start >= self.end:
            raise ValueError("start must precede end")
        if self.cameras is not None and (
            not self.cameras
            or any(not camera or len(camera) > 100 for camera in self.cameras)
        ):
            raise ValueError("cameras must contain nonempty camera names")
        return self


class AudioRepairStartBody(BaseModel):
    """Explicitly select known candidates, or confirm all scanned candidates."""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[str] | None = Field(default=None, min_length=1, max_length=500)
    all_eligible: bool = False

    @model_validator(mode="after")
    def validate_selection(self):
        """Require exactly one explicit selection mechanism."""
        if (self.item_ids is not None) == self.all_eligible:
            raise ValueError("choose item_ids or all_eligible")
        if self.item_ids is not None and (
            len(set(self.item_ids)) != len(self.item_ids)
            or any(not item or item == "*" for item in self.item_ids)
        ):
            raise ValueError("item_ids must be distinct explicit IDs")
        return self
