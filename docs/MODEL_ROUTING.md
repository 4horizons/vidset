# Vidset model-routing decisions

Prelint's documented service reviews product decisions and stores a decision ledger. It is not an inference gateway or an automatic per-request model selector. Vidset executes a deterministic routing policy locally; Prelint reviews changes to that policy in GitHub. The UI must distinguish a policy awaiting review from a verified Prelint-reviewed revision. A local fallback must never be branded as a live Prelint decision.

## Selection constraints

- Evidence analysis and verification: choose an available vision-capable adapter. OpenAI GPT-4.1 mini consumes sampled frames; Gemini through Google or fal can consume the compressed video proxy for analysis. Anthropic consumes frames. Never claim audio understanding from a frame-only model.
- Answer synthesis and edit planning: use configured reasoning preferences when available. Reuse the vision answer when the same model serves both roles.
- Still previews: use the implemented OpenAI image-editing adapter. Video models are ineligible for this operation. Create four cached stills; never four automatic videos.
- Source-video editing: choose a supported Seedance video-input adapter (Higgsfield or fal) with available credentials. Preserve subject reveal and camera movement as requested. Restore normalized source audio locally. Quality still requires visual review.
- Anchor interpolation: use the supported Kling first/last-frame route through fal, after OpenAI image editing. Do not silently treat an image-to-video model as a source-video editor.
- A retry is pinned to its original provider/model and saved request ticket. Never reroute a previously submitted paid generation.
- Unavailable credentials exclude a candidate. Configured means a key is present, not that account/model access has been verified. API failures must be surfaced honestly.
- The user chooses when a full video is generated. Navigation and preview generation never submit one.

## Review evidence

A green Prelint check is evidence of the routing policy review, not evidence that a video was visually correct or that a model is globally optimal. A check from another commit, another app, or a changed policy is insufficient. Store the checked Git commit, policy content hash, Prelint check ID and link. Never put GitHub credentials in the frontend.

Official references: https://prelint.com/docs/configuration and https://prelint.com/docs/mcp .
