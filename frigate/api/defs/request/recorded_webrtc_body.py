"""Version 1 recorded WebRTC controls, with no client-controlled media locations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from frigate.comms.live_webrtc import AnswerBody, CandidateBody, QualityBody


class RecordedWindow(BaseModel):
    """A bounded catalog window; gaps are skipped, never replaced with fake video."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    after: float = Field(ge=0)
    before: float = Field(ge=0)
    start_timestamp: float = Field(ge=0)
    stream: Literal["auto", "main", "sub"] = "auto"

    @model_validator(mode="after")
    def bounded_window(self):
        """Bound planning, copies and scheduled playback to thirty seconds."""
        if not 0.1 <= self.before - self.after <= 30:
            raise ValueError("Recording window must be between 0.1 and 30 seconds")
        if not self.after <= self.start_timestamp < self.before:
            raise ValueError("Recording start must be inside the window")
        return self


class RecordedCreate(RecordedWindow):
    version: Literal[1] = 1
    max_height: Literal[720, 1080, 2160] = 1080
    height: Literal[720, 1080, 2160] | None = None
    audio: bool = True
    force_relay: bool = False


class RecordedGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    generation: int = Field(ge=1, le=9007199254740991, strict=True)


class RecordedAnswer(AnswerBody, RecordedGeneration):
    pass


class RecordedCandidates(CandidateBody, RecordedGeneration):
    pass


class RecordedQuality(QualityBody, RecordedGeneration):
    height: Literal[720, 1080, 2160] | None = None


class RecordedControl(RecordedGeneration):
    """Activate asserts decoded warmup readiness; seek creates a new warming peer."""

    action: Literal["pause", "resume", "seek", "activate"]
    window: RecordedWindow | None = None
    decoded_ready: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def seek_window(self):
        """Reject ignored controls rather than silently pretending to apply them."""
        if (self.action == "seek") != (self.window is not None):
            raise ValueError("Only seek requires a replacement window")
        if self.action == "activate":
            if self.decoded_ready is not True:
                raise ValueError("Activation requires decoded_ready=true")
        elif self.decoded_ready is not None:
            raise ValueError("Only activation accepts decoded_ready")
        return self
