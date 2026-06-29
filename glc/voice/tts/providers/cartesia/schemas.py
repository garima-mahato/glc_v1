"""Channel-specific Pydantic types for the Cartesia TTS provider.

`CartesiaConfig` validates the `config` dict passed into
`Provider.__init__`. It is intentionally stricter than a plain dict:
prosody values are range-checked against Cartesia's own documented
bounds *before* any network call is made, so a bad config fails fast
with a clear error instead of surfacing as a confusing 400 from
upstream.

Two fields are deliberately request-shaped rather than free-form:

- `emotion` mirrors Cartesia's `generation_config.emotion`, which
  takes a list of strings (e.g. ``["positivity:high"]``); validated
  here as a list of non-empty strings, not against a closed enum,
  since Cartesia documents 60+ supported values and that list is
  expected to grow.
- `output_container` / `output_encoding` / `sample_rate` mirror
  Cartesia's `output_format` object. `container` is constrained to the
  values this adapter actually knows how to label with a correct
  `mime` type on the returned `SynthesizeResult` (see adapter.py) --
  adding a new container means updating both the `Literal` here and
  the mime-mapping there.

This model is for the *adapter's own configuration*, not the canonical
TTS result type -- `SynthesizeResult` is owned by `glc.voice.tts.base`
and is intentionally left unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Cartesia's documented model families that accept `generation_config`
#: (speed/volume/emotion). Older models (e.g. "sonic", "sonic-2") accept
#: requests but silently ignore generation_config, which would make
#: prosody overrides a no-op without any error -- so the adapter should
#: warn (not raise) if a caller combines an older model_id with
#: non-default prosody fields. See adapter.py for that check.
GENERATION_CONFIG_MODELS = ("sonic-3", "sonic-3.5")

OutputContainer = Literal["wav", "raw", "mp3"]


class CartesiaConfig(BaseModel):
    """Validated configuration for the Cartesia Sonic TTS provider.

    Instantiated from the `config` dict passed to `Provider(config=...)`.
    All fields have production-sane defaults, so `CartesiaConfig()` with
    no arguments is itself a valid, usable configuration -- no field is
    required to get a working call.
    """

    model_config = ConfigDict(extra="forbid")

    # -- Identity / routing -------------------------------------------------
    model_id: str = "sonic-3"
    """Cartesia model id. Defaults to "sonic-3" (rather than the older
    "sonic-2") specifically so `generation_config` prosody overrides are
    live by default instead of silently ignored."""

    voice_id: str | None = None
    """Explicit voice id to use when the caller does not pass one to
    `synthesize()`. Falls back to `CARTESIA_VOICE_ID` env, then a
    documented default, if left unset -- see adapter.py."""

    language: str | None = None
    """Optional Cartesia language code (e.g. "en", "fr"). Left unset to
    use the voice's own default language."""

    # -- Prosody (Cartesia's `generation_config`) ----------------------------
    speed: float = Field(1.0, ge=0.6, le=2.0)
    """Speech rate. 1.0 is Cartesia's documented default; the API's own
    valid range for sonic-3 is 0.6 (slowest) to 2.0 (fastest)."""

    volume: float = Field(1.0, ge=0.5, le=2.0)
    """Output loudness. 1.0 is default; valid range 0.5 to 2.0 per
    Cartesia's sonic-3 documentation."""

    emotion: list[str] = Field(default_factory=list)
    """Emotion tags forwarded verbatim into `generation_config.emotion`,
    e.g. ["positivity:high", "curiosity:medium"]. Empty list (default)
    means neutral delivery. Not validated against a closed set -- see
    module docstring."""

    # -- Output format (Cartesia's `output_format`) --------------------------
    output_container: OutputContainer = "wav"
    """Audio container for the response. "wav" suits browser/desktop
    playback; "raw" (headerless PCM) suits telephony pipelines such as
    Twilio that expect a bare sample stream; "mp3" trades fidelity for
    size when bandwidth is the binding constraint."""

    output_encoding: str = "pcm_s16le"
    """Sample encoding. "pcm_s16le" (16-bit signed little-endian PCM) is
    Cartesia's standard encoding and what Twilio's media streams expect
    when paired with `output_container="raw"` and `sample_rate=8000`."""

    sample_rate: int = Field(24000, gt=0)
    """Output sample rate in Hz. 24000 is a good general-purpose default
    matching Cartesia's own examples; telephony callers should override
    to 8000 to match Twilio's narrowband audio."""

    # -- Long-text handling ---------------------------------------------------
    chunk_chars: int = Field(1000, gt=0)
    """Soft threshold, in characters, above which `synthesize()` splits
    `text` on sentence boundaries and issues sequential upstream calls,
    concatenating the resulting audio. Keeps very long LLM output usable
    without requiring true session-level streaming, which this provider's
    synchronous `synthesize(text) -> SynthesizeResult` contract does not
    support. Set to a large value to effectively disable chunking."""

    # -- Operational ------------------------------------------------------------
    timeout_s: float = Field(30.0, gt=0)
    """Total request timeout, in seconds, for each upstream HTTP call.
    Cartesia's TTFA is sub-second in normal operation; a generous-looking
    30s ceiling exists only to bound worst-case hangs (e.g. a stalled
    connection), not because synthesis is expected to take that long."""

    max_retries: int = Field(0, ge=0, le=3)
    """Number of retries for transient upstream failures (connection
    errors, 429, >=500). Defaults to 0: a latency-sensitive streaming
    TTS call that already failed once has usually missed its usefulness
    window for a live conversation, so silent retrying is opt-in, not
    the default, and capped at 3 even when enabled."""

    @field_validator("emotion")
    @classmethod
    def _emotion_entries_nonempty(cls, value: list[str]) -> list[str]:
        if any(not entry.strip() for entry in value):
            raise ValueError("emotion entries must be non-empty strings")
        return value

    @field_validator("model_id")
    @classmethod
    def _model_id_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_id must not be empty")
        return value

    def generation_config_is_effective(self) -> bool:
        """True if `speed`/`volume`/`emotion` overrides will actually be
        honored by upstream given the chosen `model_id`.

        Cartesia silently ignores `generation_config` on model families
        older than sonic-3, rather than erroring -- so a caller who sets
        `model_id="sonic-2"` alongside a non-default `speed` would
        otherwise get no feedback that the override did nothing. The
        adapter uses this to decide whether to log a warning.
        """
        has_overrides = (
            self.speed != 1.0 or self.volume != 1.0 or bool(self.emotion)
        )
        return not has_overrides or self.model_id in GENERATION_CONFIG_MODELS
