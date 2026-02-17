from pydantic import BaseModel, Field


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1)
    voice: str | None = None
    language: str | None = None
    instruct: str | None = None
    max_new_tokens: int | None = Field(default=None, ge=32, le=4096)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    save_audio: bool = True


class SynthesizeResponse(BaseModel):
    provider: str
    voice: str
    language: str
    sample_rate: int
    mime_type: str = "audio/wav"
    audio_base64: str
    audio_url: str | None = None


class VoicesResponse(BaseModel):
    provider: str
    model_loaded: bool
    load_error: str | None = None
    voices: list[str]


class HealthResponse(BaseModel):
    status: str
    provider: str
    model_loaded: bool
    load_error: str | None = None


# --- Voice Profile schemas ---


class VoiceProfile(BaseModel):
    name: str
    display_name: str
    voice_type: str  # "builtin" | "cloned"
    is_owner: bool = False
    has_photo: bool = False
    created_at: str | None = None


class VoiceProfilesResponse(BaseModel):
    profiles: list[VoiceProfile]


class CloneVoiceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(min_length=1, max_length=100)
    ref_text: str = Field(min_length=1, max_length=500)
    ref_audio_base64: str
    is_owner: bool = False
    x_vector_only: bool = False


class UpdateProfileRequest(BaseModel):
    display_name: str | None = None
    is_owner: bool | None = None


class PhotoUploadRequest(BaseModel):
    photo_base64: str

