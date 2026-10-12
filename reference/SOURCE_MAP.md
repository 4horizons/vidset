# Aurrra source provenance and extraction map

Source inspected on October 11, 2026:

`/Users/jonathancruz/Desktop/content-engine`

The original repository was read only. No source files, deployment configuration, database records, or credentials were changed. No `.env` files were read or copied. No `AGENTS.md` was found in the repository or its filesystem ancestors.

## Preserved source

| File in this directory | Original | Notes |
| --- | --- | --- |
| `aurrra-main.py.snapshot` | `python-service/main.py` | Exact byte-for-byte copy of the complete 4,902-line service. Reference only; the standalone application should not import or execute this monolith. |
| `AURRRA-LICENSE` | `LICENSE` | Original proprietary Aurrra license, copied without changes. The user authorized copying code for this local isolated prototype. This copy does not grant public redistribution rights. |
| `research_prompt.txt` | `python-service/main.py:451–520` | Original `analyze_with_claude` prompt extracted using Python AST, without executing repository code. Interpolated expressions are represented by `{platform}` and `{content[:50000]}` placeholders. |

SHA256 of original `python-service/main.py` and the snapshot:

`5f7842c699e01c2bcbca7532c91a63f5a9b2af63e0dd81526074d4c57d33328e`

Before copying, the source was scanned for common provider-key prefixes, private-key material, JWT-shaped strings, and suspicious string literals assigned to credential-like names. No candidates were found. This is a source hygiene check, not a guarantee that every possible secret format could be detected.

## Existing research analysis path

The current web request enters `src/app/api/analyze/route.ts`. It checks the user/project, credit ledger, and Supabase cache, then calls `pythonFetch('/analyze-async', ...)` near line 187. `src/lib/python-service.ts` is the authenticated HTTP wrapper.

In `python-service/main.py`:

| Source lines | Responsibility |
| --- | --- |
| 218–239 | `AnalyzeRequest` and `AnalyzeAsyncRequest` request fields. |
| 331–349 | `clean_transcript` and `get_youtube_video_id`. |
| 396–423 | `fetch_youtube_transcript`: YouTube Transcript API first, `karamelo/youtube-transcripts` Apify actor fallback. Joins segments into untimed text. |
| 426–542 | Anthropic error formatting and `analyze_with_claude`. Sends up to 50,000 transcript characters to `ANTHROPIC_MODEL`, default `claude-sonnet-4-6`, and parses JSON. |
| 545–598 | `fetch_instagram_transcript`: download direct media URL using `safe_media_get`, FFmpeg mono 16kHz WAV extraction, then `transcribe_audio`. Word timestamps are flattened into text. |
| 601–690 | `run_video_analysis`: platform routing; transcript/caption fallback; output status, summary, taxonomy, hook, structure, style, CTA, title variants, and insights. |
| 1740–1754 | `/analyze`: synchronous API wrapper around `run_video_analysis`. |
| 1788–1934 | Supabase prerequisite, background analysis/save/refund helper, and `/analyze-async`. |

YouTube and YouTube Shorts require an available transcript. Instagram/TikTok use a direct media URL if supplied; if transcription fails or contains too few words, the analyzer can use the caption alone. An Instagram post-page URL is not a direct media URL.

`src/inngest/functions/analyze-video.ts` contains an older queued path that calls `/analyze` and saves results in Supabase. `execution/analyze_video.py` is a separate 396-line CLI version with similar taxonomy, but an older hardcoded Claude model and less complete Instagram media handling.

## Other useful components

- `python-service/scripts/jump_cut_vad_parallel.py:183–255`: AssemblyAI transcription with `{word, start, end}` in seconds and a local Whisper fallback. Preserve these timestamps in a video Q&A system; the original research route discards them.
- `python-service/security.py`: constrained HTTPS media URLs, approved provider hosts, public DNS address checks, redirect validation, and constant-time shared-secret comparison. Its download allowlist is specific to the deployed Aurrra services.
- `python-service/main.py:694–1194` and `1969–2165`: TubeLab YouTube discovery/outlier/channel research. Discovery is separate from analysis of a user-supplied video.
- `python-service/main.py:2168–2469`: Apify Instagram account and hashtag discovery, including profile/reel actors. This is separate from directly uploading a clip.
- `src/lib/fal-models.ts:91–135`: existing model catalog includes Kling 3 start/end-frame generation. The configured Kling image-to-video endpoints use `start_image_url` and `end_image_url`. The existing Veo image-to-video route supports only the first frame; its source comment points out that first/last-frame generation is a separate endpoint.
- `src/app/api/workflows/video-generator/route.ts:134–164`: maps selected model, start/end images, duration, aspect ratio, audio, and other capabilities into a fal queue job.
- `src/lib/fal.ts`: fal queue submit/status/result helpers, authenticated by `FAL_KEY`.
- `python-service/main.py:3763–3822`: transcription endpoint; `3825–4280`: existing conventional video edit workflow; `4375–4902`: timeline composition. These are broader than the focused prototype.

Provider names and endpoint strings above describe the inspected repository. Provider API contracts and model availability must be verified against current provider documentation when implementing new calls.

## Dependencies of the extracted analysis, without production infrastructure

For the original transcript research behavior: `anthropic`, `youtube-transcript-api`, optional `apify_client`, `requests`, and `python-dotenv`. The service wrapper uses `fastapi`, `uvicorn`, and `pydantic`. Instagram audio needs the FFmpeg binary plus `assemblyai` or the substantially larger `openai-whisper`/PyTorch fallback. Minimal upload-focused visual analysis does not need the social discovery dependencies.

The source service additionally includes unrelated Google image generation, avatars, video editing/composition, error telemetry, Supabase persistence, authentication, billing, and deployment hooks. Its entire `requirements.txt` should not be treated as the minimum environment for this prototype.

Credential names used by relevant source code include `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ASSEMBLYAI_API_KEY`, `APIFY_API_TOKEN`, `TUBELAB_API_KEY`, and `FAL_KEY`. Names are documented here; no values were inspected or copied. Local credentials should be configured independently.

## What the isolated application adds

The original research analyzer is transcript/caption analysis. It does not inspect video frames to locate physical events and cannot provide grounded screenshots for questions such as when someone picks up a tool. Timing descriptions in its content-structure output are generated prose, not validated frame timestamps.

The standalone feature therefore needs new upload/probe/frame extraction, visual indexing, evidence-aware video Q&A, localized follow-up sampling, chat history, model/provider selection, visible execution events, edit planning, boundary-frame extraction, frame editing, start/end conditioned video generation, and FFmpeg replacement/splicing. These capabilities must be identified as new implementation rather than attributed to the existing research script.

The reusable original research prompt can remain an optional transcript strategy analysis stage, while visual evidence and edit planning have separate schemas and prompts. Generated video is a model synthesis: start/end frames constrain the output but do not guarantee pixel-perfect continuity or faithful preservation of every object. The application should expose generated results for preview before exporting a replacement.

The isolated runtime should store its own assets and job state locally and avoid importing the production service, connecting to production Supabase, or depending on the original checkout at runtime.
