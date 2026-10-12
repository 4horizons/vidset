# Vidset

**Chat with a video. Find the moment. Change the scene.**

Upload a short clip, get answers with timestamps and evidence frames, then describe an edit. The app shows the models and steps as they work and saves each result alongside the original.

## Judges: start here — no setup

### [⬇ Download the judge demo (.zip)](https://github.com/4horizons/vidset/releases/download/judge-demo/VIDSET-DEMO.zip?v=understanding-your-video)

1. **Download** the ZIP above and **unzip** it.
2. **Double-click `START-HERE.html`** to open the demo in your browser. If your computer opens an editor, right-click → **Open with → Safari, Chrome, or Edge**.
3. Click **Watch the 60s demo**, or **What we built** for a quick overview.

**No server. No install. No API keys.** The download includes the videos, beach edit, evidence frames, saved Q&A, and model explanations. It works completely offline. You do not need to clone this repository or sign in to GitHub.


The beach edit and Q&A are real saved results. The model-choice step replays a real selection captured from the app after the beach edit. Prelint reviews model-routing policy; the live app applies it locally. A successful review is shown only when verified.

**We will demonstrate new uploads, live Q&A, and generation in the live app.** The HTML is a recorded presentation, not a place to submit new video edits.

## Run the live app — macOS / Linux

You need **Python 3.11+** and **FFmpeg**. On macOS, install FFmpeg with `brew install ffmpeg`; on Ubuntu, `sudo apt install ffmpeg`.

```bash
git clone https://github.com/4horizons/vidset.git
cd vidset
./run.sh
```

Open **http://127.0.0.1:8765**. The launcher installs the Python dependencies automatically. Stop it with **Ctrl+C**.

## Add keys — live app only

The fastest option is **Models & keys** in the app. Paste your keys and save. They stay in server memory until restart.

For settings that survive a restart:

```bash
cp .env.example .env
```

Open `.env` in your editor, fill the key values, and restart `./run.sh`.

| What you want to try | Key to add |
| --- | --- |
| Ask questions, plan edits, generate still previews and voice narration | `OPENAI_API_KEY` |
| Generate full source-video edits with Higgsfield | `HIGGSFIELD_API_KEY` as `KEY_ID:KEY_SECRET` |
| Use fal for source-video edits or Kling motion | `FAL_KEY` |

**OpenAI + Higgsfield is the simplest complete setup.** The app selects routes with available keys. Gemini and Anthropic are optional alternatives. A fal key works for fal-hosted routes; OpenAI still previews use a separate OpenAI key.

`.env.example` contains blank placeholders. Keep your filled `.env` on your machine; it is ignored by Git and is unnecessary for the judge demo. Live provider requests can incur charges.

## A simple live demonstration

1. Upload a short MP4 or MOV. For source-video editing, use a clip of **4–30 seconds**.
2. Tap one of the three scene-specific questions above **The conversation**, or type your own. Click a returned evidence frame to jump to its timestamp.
3. Choose **Change a moment** and describe a background change. Review the plan, then click **Generate**.
4. Show **Under the hood** for model choices and progress. Compare the result with the original.
5. Open **Video library** to return to another clip. The four environment cards create still previews; full videos start only when clicked.

## Quick questions about your video

After analysis, **Ask about this scene** suggests three questions based on the visible subjects and events. Tap one to ask it immediately in **Ask about it** and get an answer with evidence. These questions explore the original footage; edits remain in **Change a moment**.

The configured reasoning model makes one suggestion call per saved analysis, shown in **Under the hood**. Questions are cached with the video and survive reloads. Reanalyzing changed content refreshes them. If suggestions fail, type your own question or click retry.

## Turn a chat reply into narration

1. Open a completed edit, then click **Use as narration** under a chat answer. Add more replies to combine them.
2. Edit **Your script**, or use **Fit script to clip** to shorten it with your configured chat model. Review references to the original setting before narrating an edited scene.
3. Choose a voice and delivery, then click **Generate narration**. This uses your OpenAI key for `gpt-4o-mini-tts`; only the script is sent to the speech provider.
4. Click **Play narrated video**. The voice is mixed into a separate MP4, so play, pause and seeking remain synchronized. Download the video or the voice alone.

The original edit stays saved. Narration versions survive reloads and appear under **Playback audio**. Longer voice tracks hold the last frame; **Keep original audio quietly** blends the original at 12% volume. Reusing identical settings reopens the saved result; a failed local mix reuses any completed voice recording.

These are AI-generated voices. Speech generation uses the [OpenAI speech API](https://developers.openai.com/api/docs/guides/text-to-speech). Script fitting and voice generation make paid calls only when clicked. The offline judge tour remains a saved presentation.

Need more? See the [technical guide](docs/TECHNICAL_GUIDE.md), [model-routing rules](docs/MODEL_ROUTING.md), and [API contract](CONTRACT.md).
