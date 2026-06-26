# Cartesia Sonic TTS Adapter — Solution Architecture Document

**Slot:** `cartesia` (TTS, streaming-latency tier)
**Owned paths:** `glc/voice/tts/providers/cartesia/**`
**Repository:** `glc_v1` — GLC (Gateway for LLMs and Channels), Session 11
**Document owner:** Group Cartesia
**Status:** Architecture proposal, pre-implementation

---

## 1. Situation

GLC v1 is a gateway that sits between the S9 agent runtime and the outside world. Text-to-speech is one of its capabilities, exposed at `POST /v1/speak` and dispatched through `glc/voice/tts/router.py` to one of five providers selected by a `prefer` parameter:

| `prefer` | Provider | Role |
|---|---|---|
| `default` | Kokoro-82M (local) | Free, offline daily driver |
| `quality` | ElevenLabs Flash v2.5 | Wider voice palette, free-tier quota |
| `streaming` | **Cartesia Sonic** | **Lowest time-to-first-audio, for live use cases** |
| `realtime` | Gemini Live | Full-duplex voice, deferred to a later session |
| `fallback` | System TTS (`say` / `pyttsx3`) | Always-on, zero-config safety net |

The Cartesia slot exists specifically because the other four providers cannot meet the latency bar that real-time, turn-taking voice interactions require — outbound calls through `twilio_voice`, and the WebUI's voice mode. A slow TTS provider in those contexts doesn't degrade gracefully; it breaks the conversational illusion the agent is supposed to maintain. That is the gap this adapter exists to close.

At the start of this work, `glc/voice/tts/providers/cartesia/adapter.py` is an unimplemented stub: `synthesize()` raises `NotImplementedError`. The router already accounts for this — calling it with `prefer=streaming` currently raises a clean `TTSError(status=501)` pointing the caller at `prefer=fallback` — so the gateway degrades safely today, but offers none of Cartesia's latency advantage until this slot is implemented.

The class contract this adapter must satisfy is fixed by `glc/voice/tts/base.py` and is **not owned by this group**:

```python
class TTSProvider(ABC):
    name: str = ""
    def __init__(self, config: dict | None = None) -> None: ...
    async def synthesize(self, text: str, voice_id: str | None = None) -> SynthesizeResult: ...

@dataclass
class SynthesizeResult:
    audio_b64: str
    mime: str
    sample_rate: int
    provider: str
    cost_usd: float = 0.0

class TTSError(Exception):
    def __init__(self, message: str, status: int | None = None): ...
```

Any feature this adapter offers has to be reachable through this exact signature — `synthesize(text, voice_id)` plus whatever is passed into `config` at construction time. Features that would require a different method signature (for example, an incremental text-streaming input, or mid-utterance cancellation) are not implementable inside this contract without a change to `base.py`, which is out of this group's owned paths.

---

## 2. Task

Implement a production-grade `adapter.py` (and a `schemas.py` config model) for the Cartesia Sonic provider such that:

1. It satisfies the seven tests already specified in `tests/voice/tts/test_cartesia.py` (owned by core infrastructure, not editable by this group):
   - provider name matches `"cartesia"`
   - `synthesize()` returns a well-formed `SynthesizeResult`
   - the input text is forwarded to the upstream call unmodified (verified via length)
   - the returned `sample_rate` reflects what upstream actually returned, not a hardcoded constant
   - an upstream `TTSError` propagates rather than being swallowed
   - empty-string input does not crash
   - a behavioural test (`test_channel_specific_behaviour_time_to_first_audio`) checking the adapter does not introduce avoidable delay before the first byte is available
2. It calls the real Cartesia API correctly when no test mock is injected — correct auth, correct required headers, correct request/response shape — since the test suite above only ever exercises a mock and cannot itself prove the production path works.
3. It goes beyond minimum compliance to genuinely answer "how would an agent — and the person operating that agent — actually want to use this," per direct instructor guidance that this code becomes a permanent, load-bearing part of the gateway, and that the features and tests added here define how capable the agent platform (internally referred to as Arcturus) becomes downstream.
4. It is explicit, in its own documentation, about what it deliberately does *not* attempt, and why — so the boundary is a stated design decision rather than a silently discovered gap.

Non-goals, stated up front because they recur in the feature analysis below:

- **Not building barge-in / interruption handling.** A stateless `synthesize(text) -> audio` call has no notion of "currently playing audio" to interrupt — that state belongs to whatever drives playback (a channel adapter, or the full-duplex `realtime` slot). Per `docs/VOICE_GUIDE.md`, full-duplex voice sessions for `webui` and `twilio_voice` route through Gemini Live and are explicitly deferred past this session; the adapter test suite mocks that path entirely.
- **Not building streaming text-input.** Cartesia's bytes endpoint (`/tts/bytes`) takes one complete transcript per call. Incremental "feed it text as the LLM generates it" is a different Cartesia product (its WebSocket endpoint) and would require a different method signature than this slot's ABC exposes. The provider's own README is explicit that S11 scope is "a fast non-streaming Sonic request," with a chunked streaming variant deferred to a future PR against `base.py` itself.
- **Not modifying `base.py`, `router.py`, the test file, or the mock fake.** These are shared infrastructure outside this group's owned paths.

---

## 3. Action

### 3.1 Wire-level contract with the real Cartesia API

| Aspect | Decision | Source |
|---|---|---|
| Endpoint | `POST https://api.cartesia.ai/tts/bytes` | Cartesia API reference |
| Auth header | `X-API-Key: <CARTESIA_API_KEY>` | Matches the course README's documented quirk and the behavior of Cartesia's own official SDK; `Authorization: Bearer <token>` is also accepted by the raw HTTP API and is noted as a fallback in code comments |
| Required header | `Cartesia-Version: 2025-04-16` | Cartesia API reference; omitting it causes rejection, not silent staleness |
| Request body | `model_id`, `transcript`, `voice: {mode: "id", id: ...}`, `output_format: {container, encoding, sample_rate}`, optional `generation_config: {speed, volume}`, optional `language` | Cartesia API reference, course README |
| Response | Raw audio bytes, chunked transfer encoding | Cartesia API reference |

### 3.2 Dual-path dispatch: mock vs. real

```
config["mock"] present  → delegate to mock.synthesize(text, voice_id)   [unit-test path]
config["mock"] absent   → real HTTPS call to api.cartesia.ai            [production path]
```

The mock branch exists purely so `tests/voice/tts/test_cartesia.py` can exercise the adapter's contract behavior without live credentials or network access — this mirrors the pattern already used by every other provider slot in the repository and by the channel adapters described in `ADAPTER_GUIDE.md`. It is intentionally the smaller, less interesting half of the implementation. The production path is the actual deliverable.

### 3.3 Streaming consumption on the production path

Even though `/tts/bytes` is a single request/response call rather than a persistent stream, the HTTP response itself arrives as a byte stream. The adapter:

- opens the connection with `httpx.AsyncClient().stream("POST", ...)`, not `.post(...)`
- reads via `response.aiter_bytes()`, recording the wall-clock time of the first chunk internally
- joins the chunks only after the read loop completes, to assemble the single `audio_b64` blob the current `SynthesizeResult` contract requires

This is not cosmetic. It is the difference between an adapter that merely calls a fast API and one that is structured so that a future signature change (returning an async chunk iterator instead of one blob) is an additive change to this file, not a rewrite. It is also what makes the behavioural TTFA test meaningful rather than coincidentally passing.

### 3.4 Configuration surface (`schemas.py`)

A Pydantic config model replaces the currently-empty `schemas.py`, giving the adapter's `config` dict actual validation instead of trusting raw keys:

```python
class CartesiaConfig(BaseModel):
    model_id: str = "sonic-3"
    speed: float = Field(1.0, ge=0.6, le=2.0)
    volume: float = Field(1.0, ge=0.5, le=2.0)
    emotion: list[str] = Field(default_factory=list)
    output_container: Literal["wav", "raw", "mp3"] = "wav"
    output_encoding: str = "pcm_s16le"
    sample_rate: int = 24000
    chunk_chars: int = 1000
    voice_id: str | None = None
```

Values outside Cartesia's own documented ranges for `speed` and `volume` are rejected before any network call is made, surfaced as `TTSError(status=400)`.

### 3.5 Feature list, with explicit justification per feature

Every feature below is justified against one of: (a) the fixed `TTSProvider` contract, (b) a real, documented Cartesia API capability, or (c) the stated use case (latency-critical voice for live calls). Features that would require contract or scope changes outside this group's ownership are listed separately in §3.6 as acknowledged boundaries, not silently dropped.

| # | Feature | Why it belongs here |
|---|---|---|
| 1 | Mock short-circuit via `config["mock"]` | Required for the existing test suite to exercise the adapter without live credentials; matches the established pattern across all provider and channel slots |
| 2 | Real Cartesia HTTP call with correct auth/version headers | Without this, the adapter is non-functional in production regardless of test status |
| 3 | Streaming response consumption (`aiter_bytes`, first-byte timing) | Delivers the actual latency property this slot exists for; makes the TTFA behavioural test meaningful rather than accidental |
| 4 | Explicit error mapping: 401 (bad/missing key), 429 (rate limit / quota), 5xx (upstream outage), timeout → `TTSError(status=...)` | An agent or the router needs a typed status to decide whether to retry, fall back to a different `prefer=`, or surface a clear failure — not a bare exception |
| 5 | Env-var-first credential resolution (`CARTESIA_API_KEY`, `CARTESIA_VOICE_ID`), read at call time, never cached at import or in a long-lived attribute | Matches how the router actually instantiates providers (fresh, no setup call) and avoids holding a credential in a way that survives key rotation incorrectly |
| 6 | Voice resolution precedence: explicit `voice_id` argument → `config["voice_id"]` → `CARTESIA_VOICE_ID` env → documented default voice | Lets a caller override per-call without requiring every caller to know a voice ID just to get *some* audio out |
| 7 | Prosody controls — `speed`, `volume`, `emotion` exposed through `config` and forwarded into Cartesia's `generation_config` | These are real, stable (non-experimental) fields on Cartesia's `sonic-3`/`sonic-3.5` models. An agent narrating an apology versus a celebratory result has a genuine, documented way to ask for a different delivery, not just different words |
8 | Output-format override (`container`, `encoding`, `sample_rate`) | Cartesia's `output_format` genuinely supports multiple containers/encodings; a browser playback path (WAV) and a telephony path (raw PCM16 at 8kHz for Twilio) have different real requirements, and the slot's own stated use cases include both |
| 9 | Long-text chunking on sentence boundaries above a configurable threshold, with sequential calls and result concatenation | A real agent will sometimes hand TTS a multi-paragraph LLM response. Rather than erroring or truncating, splitting on natural boundaries and stitching the result keeps the adapter usable for long-form output without requiring true session-level streaming (which the contract doesn't support) |
| 10 | Fail-fast quota/rate-limit handling matching the existing `elevenlabs` slot's documented posture (`free_tier_quota_tracking`) | Consistency with an already-reviewed pattern in this codebase; lets the router's fallback chain work as intended instead of an agent hanging on a 429 |
| 11 | Cost field population (`cost_usd`) on every result, even if `0.0` on the free tier | Required by the `SynthesizeResult` contract and feeds the gateway's per-agent cost ledger; an agent or operator should never have to separately query a billing dashboard to know whether a voice turn cost anything |
| 12 | No credential ever logged, even at debug level; redaction in any future debug logging added to this file | Baseline production hygiene for an adapter that holds a live API key |

### 3.6 Acknowledged boundaries (not built, and why)

Stating these explicitly is itself part of the design, not an omission:

- **No barge-in/interruption support.** This adapter has no concept of "audio currently playing" to cancel — that state lives one layer up, in whatever drives playback. It belongs architecturally to the full-duplex `realtime` slot, which is out of scope for this session per the project's own voice guide.
- **No incremental text-streaming input.** `/tts/bytes` accepts one complete transcript; true input streaming is a different Cartesia endpoint and a different method shape than `synthesize(text, voice_id)` permits. The provider's own README states this directly: S11 scope is a fast non-streaming request, with a chunked streaming variant deferred to a future change against the base contract.
- **No SSML markup layer.** Cartesia's `speed`/`volume`/`emotion` controls cover the common expressive cases without requiring callers to author markup; phoneme-level pronunciation control (Cartesia does support pronunciation dictionaries) is left for a follow-up if a real caller needs it.
- **No persistent connection pooling or pre-warmed client across calls.** The router constructs a fresh provider instance per dispatch with no setup call; building a connection pool here would be optimizing against an assumption the router doesn't actually hold.

---

## 4. Result

### 4.1 Expected test outcomes

All seven tests in `tests/voice/tts/test_cartesia.py` pass against the mock branch:

- `test_provider_name_matches`
- `test_synthesize_returns_synthesize_result`
- `test_synthesize_passes_text_to_upstream`
- `test_synthesize_records_sample_rate`
- `test_synthesize_propagates_upstream_error`
- `test_synthesize_handles_empty_text`
- `test_channel_specific_behaviour_time_to_first_audio`

Additional local tests (not modifying owned-by-infrastructure test files) cover the features in §3.5 that the given suite does not exercise: prosody-parameter forwarding, out-of-range rejection prior to any network call, chunking behavior on long text, and output-format override propagation into the returned `mime`.

### 4.2 Expected production behavior

- A caller hitting `POST /v1/speak?prefer=streaming` receives audio with materially lower time-to-first-audio than the `quality` or `default` providers, which is the entire reason this slot exists.
- A caller can request a different emotional delivery or speaking rate for the same text without needing to know anything about Cartesia's wire format.
- A caller targeting a phone call can request telephony-appropriate audio (raw PCM16 at 8kHz) from the same method used for browser playback.
- A misconfigured deployment (missing API key) fails fast with a clear `401`-status `TTSError` rather than hanging or producing a confusing downstream error.
- A rate-limited or outage-affected upstream produces a typed, distinguishable error that the router's fallback chain (or the agent itself) can act on.

### 4.3 Verification plan

1. `pytest tests/voice/tts/test_cartesia.py -v` — confirms contract compliance via the mock.
2. `ruff check` / `mypy` on the owned path — style and type gates.
3. A manual smoke test against the real Cartesia API using a personal free-tier key, covering: a default call, a prosody-overridden call, a telephony-format call, and a deliberately long paragraph to exercise chunking — none of which the CI mock can prove on its own.
4. A recorded demonstration of the production path producing real audio, since the merged-PR requirement for this assignment explicitly calls for evidence beyond a green test suite.

### 4.4 Open items for review

- Confirm whether the grading rubric wants the `Authorization: Bearer` header as primary instead of `X-API-Key` — both are accepted by Cartesia, and the course README specifies the latter, but this is worth a one-line confirmation before merge.
- Confirm the acceptable default model tier (`sonic-2` vs `sonic-3`) given that `generation_config` (speed/volume/emotion) is only available on `sonic-3`/`sonic-3.5` — defaulting to `sonic-3` is recommended so the prosody features in §3.5 are reachable without a per-call model override.
