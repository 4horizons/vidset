# Aurrra Video Lab · Vidset

**Chat with a video. Find the moment. Change the scene.**

Upload a short clip, get answers with timestamps and evidence frames, then describe an edit. The app shows the models and steps as they work and saves each result alongside the original.

## Judges: start here — no setup

1. **[Download START-HERE.html](https://github.com/4horizons/vidset/raw/refs/heads/main/START-HERE.html).** If GitHub shows code, use **Download raw file**.
2. **Open with Safari, Chrome, or Edge**. A GitHub or Codex file preview may show source code; that is not the rendered demo. Keep the `.html` extension.
   On Mac, you can instead download this repo with **Code → Download ZIP**, unzip it, and double-click **OPEN-DEMO-MAC.command** to open Safari automatically.
3. Watch the automatic tour, or click **What we built** for a quick overview. If playback is paused, click **Play both**.

**That one file is the whole demo.** It includes both videos, the beach edit, evidence images, a saved OpenAI Q&A, and model explanations. It works offline with **no login, installation, API keys, server, or model calls**. You can share the file directly; recipients do not need GitHub access to open their copy.

The beach edit and Q&A are real saved results. The Prelint walkthrough step is explicitly **simulated**; it was added after that beach generation. Prelint reviews model-routing policy; the live app applies it locally. A successful review is shown only when verified.

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
| Ask questions, plan edits, generate still previews | `OPENAI_API_KEY` |
| Generate full source-video edits with Higgsfield | `HIGGSFIELD_API_KEY` as `KEY_ID:KEY_SECRET` |
| Use fal for source-video edits or Kling motion | `FAL_KEY` |

**OpenAI + Higgsfield is the simplest complete setup.** The app selects routes with available keys. Gemini and Anthropic are optional alternatives. A fal key works for fal-hosted routes; OpenAI still previews use a separate OpenAI key.

`.env.example` contains blank placeholders. Keep your filled `.env` on your machine; it is ignored by Git and is unnecessary for the judge demo. Live provider requests can incur charges.

## A simple live demonstration

1. Upload a short MP4 or MOV. For source-video editing, use a clip of **4–30 seconds**.
2. Ask **“What changes between the beginning and the end?”** Click a returned frame to jump to its timestamp.
3. Choose **Change a moment** and describe a background change. Review the plan, then click **Generate**.
4. Show **Under the hood** for model choices and progress. Compare the result with the original.
5. Open **Video library** to return to another clip. The four environment cards create still previews; full videos start only when clicked.

Need more? See the [technical guide](docs/TECHNICAL_GUIDE.md), [model-routing rules](docs/MODEL_ROUTING.md), and [API contract](CONTRACT.md).
