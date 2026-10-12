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

## Developer handoff: add your own keys

**No team credentials are included.** You do not need to change application code. Keys are loaded by the Python server from the `.env` file beside this README.

1. **Create your own provider keys.** Use **OpenAI + Higgsfield** for the complete flow. The requests use your accounts and billing.
2. **Create the local settings file** in the repository folder (on a fresh clone):

   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

   If `.env` already exists, edit it instead of copying over it.

3. **Open `.env` in your editor** and fill these two values. Keep the model settings from the template:

   ```dotenv
   OPENAI_API_KEY=YOUR_OPENAI_API_KEY
   HIGGSFIELD_API_KEY=YOUR_KEY_ID:YOUR_KEY_SECRET
   ```

   Higgsfield requires an **API key ID and API secret joined by a colon**. Its website login or connected ChatGPT account is not an API key. Leave unused providers blank.

4. **Start the app:** `./run.sh`. If it is already running, stop it with **Ctrl+C** first. Open **http://127.0.0.1:8765**.
5. **Check Models & keys.** OpenAI and Higgsfield should show as configured. Try the illustrated sample first; it makes no model calls. Upload a real short clip when ready—analysis and four still previews can start automatically and incur provider charges. Full video generation and narration start when clicked.

| Environment variable | What it enables |
| --- | --- |
| `OPENAI_API_KEY` | Video questions, suggested questions, edit planning, still previews, and voice narration |
| `HIGGSFIELD_API_KEY` | Full source-video editing; format `KEY_ID:KEY_SECRET` |
| `FAL_KEY` | Optional video editing through fal, or Kling between edited anchor frames |
| `GEMINI_API_KEY` | Optional Gemini analysis and reasoning |
| `ANTHROPIC_API_KEY` | Optional Anthropic analysis and reasoning |

**Using fal instead of Higgsfield?** Add `FAL_KEY` and set `VIDEO_EDIT_PROVIDER=fal` in `.env`. Keep OpenAI for still previews and narration. Gemini and Anthropic can be selected in **Models & keys**; their keys are separate from OpenAI and fal keys.

**Replacing or removing keys:** stop the server, replace or blank the values in `.env`, and restart. Keys pasted into **Models & keys** last only until the server stops; leaving a UI field blank does not clear an existing key. Exported shell environment variables override `.env`, so unset old provider variables in the launching terminal if the app still finds an old key. Stop any separate shared server/tunnel too when taking an instance offline. Removing a local key does not revoke it—revoke an unused key in its provider account when you no longer need it elsewhere.

Keep `.env` private: it is ignored by Git, is never required for the offline judge demo, and must not be added to frontend files or shared downloads. Only `.env.example`, with blank credentials, belongs in the repository. Saved videos and edits remain in `data/` when keys are changed.

## A simple live demonstration

1. Upload a short MP4 or MOV. For source-video editing, use a clip of **4–30 seconds**.
2. Tap one of the three scene-specific questions above **The conversation**, or type your own. Click a returned evidence frame to jump to its timestamp.
3. Choose **Change a moment** and describe a background change. Review the plan, then click **Generate**.
4. Show **Under the hood** for model choices and progress. Compare the result with the original.
5. Open **Video library** to return to another clip. The four environment cards create still previews; full videos start only when clicked.

For a brief moment, leave the edit times on **Auto**. Source editing adds surrounding footage to meet its four-second minimum, shows the target moment separately, and asks the model to preserve the added context. Review the plan before generating. Explicit start/end times are kept as entered and must meet the selected mode's limits.

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
