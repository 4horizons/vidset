# Internal app contract

Local FastAPI + buildless JS UI, one origin on 127.0.0.1:8765. No production repo imports or secrets copied. Backend API prefix `/api`. All media uses `/media/{video_id}/...`. API errors have `{detail: string}`. JSON request/response unless upload. Jobs polled every 1 second.

## Configuration
GET `/api/config` returns `{providers:{gemini:{configured:bool},openai:{configured:bool},anthropic:{configured:bool},fal:{configured:bool},higgsfield:{configured:bool}}, models:{vision_provider:'gemini',vision_model:'gemini-2.5-flash',chat_provider:'openai',chat_model:'gpt-4.1-mini',image_model:'gpt-image-1.5',video_model:'fal-ai/kling-video/o1/image-to-video',video_edit_model:'bytedance/seedance-2.5/video-edit'}, limits:{max_mb:128,max_seconds:120}, ffmpeg:bool}`.
POST `/api/config` JSON `{keys?:{gemini?:string,openai?:string,anthropic?:string,fal?:string,higgsfield?:string},models?:{vision_provider,vision_model,chat_provider,chat_model,image_model,video_model,video_edit_model}}`; keys stay server memory only. Empty/missing keys preserve prior values. Provider choices gemini/openai/anthropic. Model IDs editable. Returns same shape as GET.

## Video
POST `/api/videos` multipart `file` -> `{job_id}`. POST `/api/demo` -> `{job_id}` creates clearly labeled synthetic sample (no AI). GET `/api/videos` -> `{videos:[Video]}`. GET `/api/videos/{id}` -> Video.
Video = `{id,name,duration,width,height,fps,has_audio,url,thumbnail,frames:[{timestamp,url}],analysis:null|{summary,events:[{start,end,label,description}],limitations,provider,model},demo:bool}`.
Upload job result `{video:Video}`. Video normalized to MP4 and frames extracted locally.

## Jobs + chat
GET `/api/jobs/{id}` -> `{id,type,status:'queued'|'running'|'completed'|'failed',events:[{id,stage,provider,model,status:'running'|'completed'|'failed'|'info',message,time,elapsed_ms?,artifacts?:[{url,label,timestamp?}]}],result:null|object,error:null|string}`. Events append-only, model names always actual. UI should visibly show stage/provider/model/status and artifacts. No chain-of-thought, only observable steps and brief decision summaries.
POST `/api/videos/{id}/analyze` -> `{job_id}`. Result `{video:Video}`.
POST `/api/videos/{id}/chat` `{message,history?:[{role:'user'|'assistant',content:string}]}` -> `{job_id}`. Result `{answer,evidence:[{timestamp,url,caption}],suggested_range:null|{start,end},provider,model,limitations:string}`. With no keys arbitrary uploads cannot get AI answers; demo only uses explicit scripted responses.

## Editing
POST `/api/videos/{id}/edits/plan` `{instruction,start?:number,end?:number,strategy?:'anchors'|'source_video'}` -> `{job_id}`. Time fields optional: model locates from analysis. Result `{edit:Edit}`. Inputs in seconds: anchors 1–10 seconds; source_video 4–30 seconds, within source. API default anchors for backwards compatibility; UI default source_video for real uploads. Demo always anchors.
Edit = `{id,video_id,strategy,instruction,start,end,status:'planned'|'generating'|'completed'|'failed',rationale,prompt,anchors:{start:{timestamp,url},end:{timestamp,url}},edited_anchors?:{start:{url},end:{url}},replacement_url?:string,output_url?:string,source_clip_url?:string,provenance?:string,review?:string,demo:bool}`.
POST `/api/videos/{id}/edits/{edit_id}/generate` -> `{job_id}`. Explicit button starts paid generation. Source-video strategy requires only the Higgsfield API key pair for generation: silent selected clip → Seedance2.5 → FFmpeg restores audio. Anchor strategy: edit first+last anchor images with OpenAI; Kling through fal.ai fills segment; FFmpeg replaces segment and retains original full audio. Result `{edit:Edit}`. Demo generation performs a clearly labeled deterministic graphics change, never claims model use.
GET `/api/videos/{id}/edits` -> `{edits:[Edit]}`.

## Frontend requirements
Implement app/static/index.html, app.js, styles.css only. Branded Vidset / Video Lab. Refined editing studio: light cream outside/dark video player, lime green accent, nice typography using local/system fonts, 3 panel workspace video+timeline, conversation, activity. Responsive. Empty upload state, demo sample action, drag drop, native playback, seekable evidence, questions, ask vs edit mode, manual start/end controls, plan review with original anchors and prompt, explicit Generate replacement CTA, edited output player/download, settings dialog with keys and model selectors and configured indicators. Live activity feed actual events, model roster. Initial GET config/videos restores available assets; never invent successful API result. Clear loading/errors, aria labels, keyboard access. No framework/build/npm needed. Use textContent for user/model content. The backend runs async jobs; poll, accumulate visible event feed. Analyze manually via button or after upload if a configured provider exists; demo can auto analyze. Show short copy explaining external API processing when configured and demo is scripted.

## Providers module contract (agent-owned)
`app/providers.py`: implement class ProviderError(Exception) and async functions:
`analyze_video(config:dict, video_path:Path, frames:list[dict], duration:float, emit:async callback) -> dict` returning `{summary,events:[{start,end,label,description}],limitations,provider,model}`.
`answer_question(config, question:str, analysis:dict, frames:list[dict], duration:float, history:list, emit) -> dict` returning `{answer,evidence:[{timestamp,caption}],suggested_range:null|{start,end},limitations,provider,model}`. Includes multimodel visual verification with vision provider then chat synthesis when distinct/configured. frames entries `{timestamp,path:Path,url}` selected local evidence. Frame ground labels bound timestamps.
`plan_edit(config,instruction:str,analysis:dict,duration:float,start:float|None,end:float|None,emit,strategy="anchors") -> dict` returns `{start,end,rationale,prompt}`.
`edit_image(config,source_path:Path,output_path:Path,instruction:str,emit,label:str) -> None` OpenAI image edit.
`generate_video(config,start_path:Path,end_path:Path,output_path:Path,prompt:str,duration:float,emit) -> None` fal queue Kling first/last frames via data URIs; safely download returned video, timeout.
config = `{keys:{gemini,openai,anthropic,fal,higgsfield},models:{...above}}`; emit called `await emit(stage,provider,model,message,status='running',artifacts=None)` (elapsed backend automatic). Keys never log, SDK responses/errors sanitized. HTTPX REST; no SDK needed; PIL available. Gemini analyze uses inline whole video (backend supplies small proxy <=18MB), other providers labeled sampled frame only. Explicit no-key errors. JSON robust parse. Can use configurable model IDs. Verify official REST docs online. Agent write tests/providers tests if useful under tests/test_providers.py. Do not edit other files.

`app/source_editor.py`: `edit_source_video(config,source_path,output_path,prompt,duration,emit)` submits one Seedance 2.5 source edit with durable idempotency. Emits actual upload/queue/generation events; does not edit images or invent progress.


## Suggested analysis questions
- `GET /api/videos/{video_id}/questions`: cached `status`, `questions`, provider/model, and saved workflow events. Read-only; never calls a model.
- `POST /api/videos/{video_id}/questions`: requires saved analysis; returns a job with three questions for understanding the original footage. Uses the configured reasoning route. No edit plan or video is generated.
- Completed suggestions are cached by analysis fingerprint. Concurrent requests reconnect to the same job; changed analysis invalidates the cache. Failed/interrupted suggestions require a retry. The illustrated demo uses known fixture questions without API calls.
- The UI asks a selected question through the existing `/chat` endpoint and displays its evidence frames.

## Voice narration
- `POST /api/videos/{video_id}/edits/{edit_id}/narration-script`: `{text}` → async job returning `{script, provider, model}` using the configured reasoning route.
- `POST /api/videos/{video_id}/edits/{edit_id}/narrations`: `{script, voice, style, keep_original}` → async job returning `{narration}`. Requires a completed edit. OpenAI speech (`gpt-4o-mini-tts`) followed by local FFmpeg assembly; the original remains unchanged.
- `GET /api/videos/{video_id}/edits/{edit_id}/narrations`: saved versions and statuses. Identical script/voice/style/mix settings reuse a completed version or reconnect to an active job; saved WAVs are reused after local assembly failure.
- Narrations contain `output_url`, `audio_url`, `duration`, `speech_duration`, `hold_seconds`, `run_events`, and `workflow`. WAV assets are same-origin, scoped to their video, and support range requests.
- Narration is AI-generated. Script length is limited to 2,000 characters; voice duration is bounded at 180 seconds. Long voiceovers hold the final video frame, never trim spoken words. Original audio is optionally mixed at 12% volume.
