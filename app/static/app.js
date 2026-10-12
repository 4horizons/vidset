"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const names = { gemini: "Google Gemini", openai: "OpenAI", anthropic: "Anthropic", fal: "fal.ai", higgsfield: "Higgsfield", local: "Local", demo: "Scripted demo", ffmpeg: "FFmpeg" };
  const credentialProviders = ["gemini", "openai", "anthropic", "fal", "higgsfield"];
  const state = {
    config: null, videos: [], video: null, mode: "ask", strategy: "source_video", busy: false,
    conversations: new Map(), events: [], seenEvents: new Set(),
    selectionSequence: 0, toastTimer: null, dragDepth: 0,
    savedEdits: new Map(), sceneSaveTimer: null, storageNotice: false,
    environments: [], presetSelection: null, gallerySignature: "", galleryPolling: false, previews: new Map(), previewRequests: new Set(), routing: null, routingSignature: "",
  };
  const flow = { job: null, saved: false, view: null, open: new Map(), previous: new Map(), collapseTimers: new Map(), tick: null, signature: "" };
  const elements = {
    video: $("#source-video"), output: $("#output-video"), input: $("#message-input"),
    messages: $("#messages"), conversation: $("#conversation-body"),
  };
  const safeMedia = (value) => {
    if (typeof value !== "string" || !value) return "";
    try {
      const url = new URL(value, window.location.origin);
      return url.origin === window.location.origin && ["http:", "https:"].includes(url.protocol) && url.pathname.startsWith("/media/") ? url.href : "";
    } catch { return ""; }
  };
  const asText = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
  const time = (value, precise = false) => {
    const seconds = Math.max(0, Number(value) || 0);
    const units = precise ? 10 : 1;
    const total = precise ? Math.round(seconds * units) : Math.floor(seconds);
    const minutes = Math.floor(total / (60 * units));
    const remainder = precise ? ((total % 600) / 10).toFixed(1).padStart(4, "0") : (total % 60).toString().padStart(2, "0");
    return `${minutes.toString().padStart(2, "0")}:${remainder}`;
  };
  const icon = (name, small = true) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", `icon${small ? " small" : ""}`);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
  };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const isConfigured = (provider) => Boolean(state.config?.providers?.[provider]?.configured);
  const canAnalyze = () => Boolean(state.video && (state.video.demo || isConfigured(state.config?.models?.vision_provider)));
  const messages = () => state.video ? state.conversations.get(state.video.id) || [] : [];
  const currentStrategy = () => state.video?.demo ? "anchors" : state.strategy;
  const isSourceEdit = (edit) => !edit.demo && edit.strategy === "source_video";
  const sourceProvider = () => state.config?.models?.video_edit_provider === "fal" ? "fal" : "higgsfield";
  const presetName = (id) => state.environments.find((item) => item.id === id)?.name || ({"golf-course":"Golf course",festival:"Music festival",courtside:"Courtside seats",airport:"Airport lounge"})[id];
  const safeExample = (value) => {
    if (typeof value !== "string") return "";
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && url.pathname.startsWith("/static/environment-examples/") ? url.href : "";
    } catch { return ""; }
  };

  // Media and edits live on the server; only this browser's conversation and
  // viewing preferences are cached here. Provider configuration is never stored.
  // Retain the original storage keys so the Vidset rebrand preserves saved conversations.
  const sceneKey = (id) => `aurrra.video-lab.scene.v1.${id}`;
  const activeKey = "aurrra.video-lab.active.v1";
  function storedScene(id) {
    try {
      const value = JSON.parse(localStorage.getItem(sceneKey(id)) || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch { return {}; }
  }
  function storedMessages(id) {
    const value = storedScene(id).messages;
    return Array.isArray(value) ? value.filter((item) => item && ["user", "assistant"].includes(item.role) && typeof item.text === "string" && !item.edit && !item.pending).slice(-60) : [];
  }
  function saveCurrentScene() {
    if (!state.video) return;
    const prior = storedScene(state.video.id);
    const snapshot = {
      draft: elements.input.value.slice(0, 4000), mode: state.mode, strategy: state.strategy,
      start: $("#range-start").value, end: $("#range-end").value,
      playhead: elements.video.readyState ? elements.video.currentTime : Number(prior.playhead) || 0,
      messages: messages().filter((item) => !item.pending && !item.edit).slice(-60).map((item) => ({
        role: item.role, text: item.text, mode: item.mode, error: item.error,
        evidence: item.evidence, suggested_range: item.suggested_range,
        model: item.model, provider: item.provider, limitations: item.limitations,
      })),
    };
    try {
      localStorage.setItem(sceneKey(state.video.id), JSON.stringify(snapshot));
      localStorage.setItem(activeKey, state.video.id);
    } catch {
      if (!state.storageNotice) {
        state.storageNotice = true;
        toast("Videos and edits remain saved. This browser could not save the conversation and viewing position.", true);
      }
    }
  }
  function scheduleSceneSave() {
    clearTimeout(state.sceneSaveTimer);
    state.sceneSaveTimer = setTimeout(saveCurrentScene, 350);
  }

  async function api(path, { method = "GET", body } = {}) {
    let response;
    try {
      response = await fetch(path, { method, headers: body instanceof FormData ? {} : body !== undefined ? { "Content-Type": "application/json" } : {}, body: body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body) });
    } catch { throw new Error("The local server is unreachable. Make sure Vidset is still running."); }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.detail;
      const error = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((item) => item.msg || "Invalid request").join(" · ") : payload.error || `Request failed (${response.status})`;
      throw new Error(asText(error));
    }
    return payload;
  }

  function toast(message, error = false) {
    clearTimeout(state.toastTimer);
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("error", error);
    node.hidden = false;
    state.toastTimer = setTimeout(() => { node.hidden = true; }, error ? 9000 : 4500);
  }

  function updateControls() {
    const hasVideo = Boolean(state.video);
    elements.input.disabled = !hasVideo || state.busy;
    $("#send-button").disabled = !hasVideo || state.busy || !elements.input.value.trim();
    $("#analyze-button").disabled = !hasVideo || state.busy;
    $("#asset-select").disabled = state.busy;
    ["#upload-button", "#new-upload", "#demo-button", "#settings-button", "#library-upload"].forEach((id) => { $(id).disabled = state.busy; });
    document.querySelectorAll(".library-card").forEach((button) => { button.disabled = state.busy; });
    document.querySelectorAll(".environment-action").forEach((button) => { button.disabled = state.busy; });
    ["#range-start", "#range-end"].forEach((id) => { $(id).disabled = state.busy; });
    $("#edit-strategy").disabled = state.busy || Boolean(state.video?.demo);
    document.querySelectorAll(".generate-button").forEach((button) => { button.disabled = state.busy || button.dataset.finished === "true"; });
    $("#composer-hint").textContent = !hasVideo ? "Upload a video to begin" : state.busy ? "Working on your scene…" : state.mode === "edit" ? "First we’ll find and review the edit" : state.video.demo ? "Scripted sample · no model calls" : "Grounded in frames and timestamps";
    $("#send-button").setAttribute("aria-label", state.mode === "edit" ? "Plan the video edit" : "Ask about this video");
    const model = state.config?.models;
    $("#conversation-provider").textContent = state.video?.demo ? "DEMO · deterministic, no AI calls" : model ? `${names[model.chat_provider] || model.chat_provider} · ${model.chat_model}` : "Your models. Your perspective.";
    $("#activity-indicator").classList.toggle("busy", state.busy);
  }

  function setBusy(value, label = "") {
    state.busy = value;
    if (!flow.job) $("#activity-status").textContent = value ? label || "Working on your scene" : "Ready for this scene";
    updateControls();
    if (flow.job) updateWorkflow(flow.job, { saved: flow.saved });
  }

  function setMode(mode) {
    state.mode = mode;
    if (!state.busy) renderRouting();
    for (const option of ["ask", "edit"]) {
      const button = $(`#${option}-tab`);
      button.classList.toggle("active", option === mode);
      button.setAttribute("aria-selected", String(option === mode));
      button.tabIndex = option === mode ? 0 : -1;
    }
    elements.conversation.setAttribute("aria-labelledby", `${mode}-tab`);
    $("#edit-controls").hidden = mode !== "edit";
    elements.input.placeholder = mode === "edit" ? "Describe the change you imagine…" : "What would you like to know?";
    const editPrompts = state.video?.demo ? [
      ["Change the red hydrant to yellow while keeping the scene otherwise unchanged.", "Make the hydrant yellow"],
      ["Add a swing hanging from the tree in the background.", "Add a swing to the tree"],
      ["Change the color of an object in the main action, keeping the camera motion the same.", "Reimagine one small detail"],
    ] : [
      ["Replace the background with a natural, sunlit beach. Preserve the main subject, distinctive details, movement, and camera motion.", "Move the scene to a beach"],
      ["Change one background object while preserving the main subject and their movement.", "Reimagine a background detail"],
      ["Keep the scene and action the same, but change the lighting to warm golden hour.", "Try golden-hour lighting"],
    ];
    const prompts = mode === "edit" ? editPrompts : [
      ["What changes between the beginning and the end?", "What changes from start to finish?"],
      ["Describe the key moments with timestamps.", "Show me the key moments"],
      ["When does the main action happen?", "When does the main action happen?"],
    ];
    $("#suggestions").replaceChildren(...prompts.map(([prompt, label]) => {
      const button = el("button", "", label);
      button.type = "button";
      button.dataset.prompt = prompt;
      button.append(el("span", "", "↗"));
      return button;
    }));
    $("#chat-empty h3").textContent = mode === "edit" ? "A small change. A new scene." : "Look a little closer.";
    $("#chat-empty > p").textContent = mode === "edit" ? "Describe one change. Review the source moment and boundary evidence before generating your edit." : "Ask what happens, find a specific action, or reimagine something in the scene.";
    renderStrategy();
    updateControls();
  }

  function renderStrategy() {
    const strategy = currentStrategy();
    $("#edit-strategy").value = strategy;
    $("#edit-strategy-help").textContent = state.video?.demo
      ? "The scripted sample uses the anchor workflow and makes no AI calls."
      : strategy === "source_video"
        ? `${names[sourceProvider()]} edits the original clip, using its motion as guidance. Boundary frames are evidence; the source audio is preserved.`
        : "OpenAI edits the first and last frames. fal.ai generates the footage between them, then the source audio is restored.";
    $("#range-help").textContent = strategy === "source_video" ? "A source video edit can span 4–30 seconds." : "An anchor replacement can span 1–10 seconds.";
    renderRoster();
  }

  function renderRoster() {
    const models = state.config?.models;
    if (!models) return;
    const roles = [
      ["◈", "VISION & FRAME EVIDENCE", models.vision_provider, models.vision_model],
      ["✳", "REASONING & EDIT PLANNING", models.chat_provider, models.chat_model],
    ];
    if (currentStrategy() === "source_video") {
      const provider = sourceProvider();
      const model = provider === "fal" ? models.fal_video_edit_model || "bytedance/seedance-2.5/reference-to-video" : models.video_edit_model || "bytedance/seedance-2.5/video-edit";
      roles.push(["↗", `SOURCE VIDEO EDIT · ${names[provider].toUpperCase()}`, provider, model]);
    }
    else roles.push(["▧", "ANCHOR IMAGE EDIT · OPENAI", "openai", models.image_model], ["↗", "BETWEEN-ANCHOR VIDEO · FAL.AI", "fal", models.video_model]);
    $("#model-roster").replaceChildren(...roles.map(([mark, role, provider, model]) => {
      const row = el("div", "model-row");
      const info = el("div", "model-info");
      info.append(el("strong", "", model), el("span", "", role));
      const connected = isConfigured(provider);
      const dot = el("span", `connection-dot${connected ? " connected" : ""}`);
      dot.title = `${names[provider] || provider}: ${connected ? "connected" : "no key configured"}`;
      dot.setAttribute("aria-label", dot.title);
      row.title = `${names[provider] || provider} · ${model} · ${connected ? "Key configured" : "Connect in Models & keys"}`;
      row.append(el("div", "model-icon", mark), info, dot);
      return row;
    }));
    $("#upload-limits").textContent = `Up to ${state.config.limits?.max_seconds || 120} seconds · ${state.config.limits?.max_mb || 128} MB`;
    updateControls();
  }

  function renderAssetSelect() {
    $("#asset-select").replaceChildren(...state.videos.map((video) => {
      const option = el("option", "", `${video.demo ? "SAMPLE · " : ""}${video.name}`);
      option.value = video.id;
      return option;
    }));
    if (state.video) $("#asset-select").value = state.video.id;
    $("#asset-toolbar").hidden = state.videos.length === 0;
    $("#library-count").textContent = String(state.videos.length);
    if ($("#library-dialog").open) renderLibrary();
  }

  function renderLibrary() {
    const query = $("#library-search").value.trim().toLowerCase();
    const videos = state.videos.filter((video) => `${video.name} ${video.analysis?.summary || ""}`.toLowerCase().includes(query));
    const grid = $("#library-grid");
    grid.replaceChildren();
    $("#library-count").textContent = String(state.videos.length);
    $("#library-status").textContent = state.busy ? "A video is processing. You can open another one when it finishes." : `${videos.length} ${videos.length === 1 ? "video" : "videos"}${query ? " found" : " saved"} · select a scene to pick up where you left off`;
    if (!videos.length) {
      const empty = el("div", "library-empty");
      empty.append(icon("film", false), el("strong", "", query ? "No matching videos" : "Your first scene starts here"), el("p", "", query ? "Try a filename or a detail from its analysis." : "Upload a video. Its source and edits will stay here."));
      grid.append(empty);
    }
    for (const video of videos) {
      const selected = state.video?.id === video.id;
      const card = el("button", `library-card${selected ? " selected" : ""}`);
      card.type = "button";
      card.disabled = state.busy;
      card.dataset.videoId = video.id;
      card.setAttribute("aria-label", `Open ${video.name}${selected ? " (current video)" : ""}`);
      const preview = el("div", "library-preview");
      const image = el("img");
      image.src = safeMedia(video.frames?.at(-1)?.url || video.thumbnail);
      image.alt = `Saved frame from ${video.name}`;
      image.loading = "lazy";
      preview.append(image, el("span", "library-duration", time(video.duration)));
      if (selected) preview.append(el("span", "library-current", "CURRENT VIDEO"));
      const body = el("div", "library-card-body");
      body.append(el("strong", "library-name", video.name), el("p", "library-summary", video.analysis?.summary || "Ready to analyze. Ask a question or describe an edit."));
      const badges = el("div", "library-badges");
      badges.append(el("span", "", video.demo ? "Scripted sample" : video.analysis ? "Analyzed" : "New upload"));
      const edits = state.savedEdits.get(video.id);
      if (edits?.length) {
        const completed = edits.filter((edit) => edit.status === "completed" && edit.output_url).length;
        badges.append(el("span", "has-edits", completed ? `${completed} finished ${completed === 1 ? "edit" : "edits"}` : `${edits.length} saved ${edits.length === 1 ? "plan" : "plans"}`));
      }
      body.append(badges, el("span", "library-open", selected ? "Continue working ↗" : "Open workspace ↗"));
      card.append(preview, body);
      card.addEventListener("click", async () => {
        if (state.busy) return;
        card.disabled = true;
        try {
          const fresh = await api(`/api/videos/${encodeURIComponent(video.id)}`);
          await selectVideo(fresh);
          $("#library-dialog").close();
          toast(`Opened ${video.name}. Your saved edits are ready.`);
        } catch (error) { toast(error.message, true); card.disabled = false; }
      });
      grid.append(card);
    }
  }

  function renderEnvironments(force = false) {
    $("#environment-gallery").hidden = !state.environments.length;
    const edits = state.savedEdits.get(state.video?.id) || [];
    const previewSet = state.previews.get(state.video?.id);
    const signature = JSON.stringify([state.video?.id, state.environments, previewSet, edits.map((edit) => [edit.id, edit.preset_id, edit.status, edit.output_url])]);
    if (!force && state.gallerySignature === signature) return;
    state.gallerySignature = signature;
    const real = state.video && !state.video.demo;
    $("#environment-intro").textContent = real ? `Still previews of ${state.video.name}. Choose one to generate its video.` : "Explore the can stills, then upload a video to preview your own settings.";
    const grid = $("#environment-cards");
    grid.replaceChildren(...state.environments.map((preset) => {
      const matching = edits.filter((edit) => edit.preset_id === preset.id);
      const completed = matching.find((edit) => edit.status === "completed" && edit.output_url);
      const active = matching.find((edit) => edit.status === "generating");
      const planned = matching.find((edit) => edit.status === "planned");
      const still = previewSet?.previews?.find((item) => item.id === preset.id);
      const pending = ["running", "queued"].includes(still?.status) || ["running", "queued"].includes(previewSet?.status) && !still;
      const own = safeMedia(still?.url);
      const poster = own || (!real ? safeExample(preset.poster_url) : "");
      const card = el("article", `environment-card environment-${preset.color}`);
      const preview = el("div", "environment-preview");
      if (poster) {
        const image = el("img"); image.src = poster; image.alt = `${preset.name} · ${own ? "still preview of your video" : "LaCroix still example"}`;
        preview.append(image);
      } else {
        const concept = el("div", "environment-concept");
        concept.append(el("span", "environment-glyph", ({golf:"⚑",festival:"✦",courtside:"◉",airport:"↗"})[preset.color]), el("span", "", pending ? "Creating a still preview…" : still?.status === "failed" ? "Still preview unavailable" : "A new place for your scene"));
        preview.append(concept);
      }
      preview.append(el("span", "environment-preview-label", own ? "AI STILL PREVIEW" : poster ? "LACROIX STILL EXAMPLE" : "SETTING IDEA"));
      const content = el("div", "environment-content");
      content.append(el("span", "environment-eyebrow", preset.eyebrow), el("h3", "", preset.name), el("p", "", preset.description));
      const status = el("p", "environment-render-status", pending ? `OpenAI · ${previewSet?.model || "image model"}` : own ? `Source frame ${Number(still.timestamp).toFixed(2)}s · video on click` : real && !isConfigured("openai") ? "Connect OpenAI for still previews" : "Video generation starts only when you click");
      if (pending) status.prepend(el("span", "environment-spinner"));
      const button = el("button", "environment-action button button-outline", completed ? "Open saved video ↗" : active ? "View video progress ↗" : "Generate video ↗");
      button.type = "button"; button.disabled = state.busy;
      button.addEventListener("click", () => {
        if (completed) { renderOutput(completed); return; }
        if (active) { showSavedEdit(active); return; }
        generateEnvironment(preset, planned);
      });
      content.append(status, button);
      if (still?.status === "failed" || previewSet?.status === "failed" && !own) {
        const retry = el("button", "environment-again", "Retry missing still previews");
        retry.type = "button"; retry.title = still?.error || previewSet.error || "Retry image generation";
        retry.addEventListener("click", () => requestEnvironmentPreviews(true)); content.append(retry);
      }
      card.append(preview, content);
      return card;
    }));
  }

  function showSavedEdit(edit) {
    if (!messages().some((item) => item.edit?.id === edit.id)) messages().push({ role: "assistant", text: `${presetName(edit.preset_id) || "Saved"} edit`, edit });
    renderMessages();
    $(".conversation-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    if (edit.status === "generating" && edit.job_id) watchSavedGeneration(edit);
  }

  async function watchSavedGeneration(edit) {
    if (state.busy) return;
    const videoId = state.video.id;
    setBusy(true, "Following your saved generation");
    try {
      while (state.video?.id === videoId) {
        const job = await api(`/api/jobs/${encodeURIComponent(edit.job_id)}`);
        recordEvents(job);
        if (job.status === "failed") throw new Error(job.error || "Generation stopped.");
        if (job.status === "completed") {
          Object.assign(edit, job.result.edit);
          renderOutput(edit); renderMessages(); renderEnvironments(true); return;
        }
        await wait(1500);
      }
    } catch (error) { toast(error.message, true); }
    finally { setBusy(false); }
  }

  async function generateEnvironment(preset, planned) {
    if (state.busy) return;
    if (!state.video || state.video.demo) { $("#file-input").click(); toast("Upload your own video to generate this setting."); return; }
    if (Number(state.video.duration) < 4) { toast("Use a clip at least four seconds long for source-video setting changes."); return; }
    const videoId = state.video.id;
    state.strategy = "source_video";
    setMode("edit");
    setRange(0, Math.min(30, state.video.duration));
    $(".conversation-panel").scrollIntoView({ behavior: "smooth", block: "center" });
    try {
      let edit = planned;
      if (!edit) {
        const result = await runJob(`/api/videos/${encodeURIComponent(videoId)}/edits/plan`, { instruction: preset.instruction, strategy: "source_video", start: 0, end: Math.min(30, state.video.duration), preset_id: preset.id }, `Planning the ${preset.name.toLowerCase()} video`);
        edit = result.edit;
        state.savedEdits.set(videoId, [edit, ...(state.savedEdits.get(videoId) || [])]);
      }
      showSavedEdit(edit);
      // Only this explicit Generate video click enters the video-generation path.
      await generateEdit(edit);
    } catch (error) { toast(error.message, true); }
    finally { renderEnvironments(true); }
  }

  async function requestEnvironmentPreviews(retry = false) {
    const videoId = state.video?.id;
    if (!videoId || state.video.demo || !isConfigured("openai") || state.previewRequests.has(videoId)) return;
    state.previewRequests.add(videoId);
    try {
      await api(`/api/videos/${encodeURIComponent(videoId)}/environment-previews${retry ? "?retry_failed=true" : ""}`, { method: "POST" });
      const result = await api(`/api/videos/${encodeURIComponent(videoId)}/environment-previews`);
      state.previews.set(videoId, result);
      if (state.video?.id === videoId) renderEnvironments();
    } catch (error) {
      state.previews.set(videoId, { status: "failed", previews: [], error: error.message });
      toast(`Still previews: ${error.message}`, true);
    } finally { state.previewRequests.delete(videoId); }
  }

  async function refreshEnvironments() {
    if (state.galleryPolling || document.hidden) return;
    state.galleryPolling = true;
    const videoId = state.video?.id;
    try {
      const [presets, saved, previews] = await Promise.all([
        api("/api/environments"),
        videoId ? api(`/api/videos/${encodeURIComponent(videoId)}/edits`) : Promise.resolve(null),
        videoId ? api(`/api/videos/${encodeURIComponent(videoId)}/environment-previews`) : Promise.resolve(null),
      ]);
      state.environments = presets.environments || [];
      if (saved && state.video?.id === videoId) {
        state.savedEdits.set(videoId, saved.edits || []);
        state.previews.set(videoId, previews);
        for (const message of messages()) {
          const fresh = saved.edits.find((edit) => edit.id === message.edit?.id);
          if (fresh) message.edit = fresh;
        }
        if (previews.status === "not_started" && !state.busy) await requestEnvironmentPreviews();
        if (previews.job_id && !state.busy && ["queued", "running"].includes(previews.status)) {
          const job = await api(`/api/jobs/${encodeURIComponent(previews.job_id)}`);
          if (state.video?.id === videoId && !state.busy) recordEvents(job);
        } else if (previews.job_id === flow.job?.id && !state.busy) {
          const job = await api(`/api/jobs/${encodeURIComponent(previews.job_id)}`).catch(() => null);
          if (job && state.video?.id === videoId) recordEvents(job);
        }
      }
      renderEnvironments();
    } catch { /* A failed gallery refresh must not interrupt an active edit. */ }
    finally { state.galleryPolling = false; }
  }

  async function openLibrary() {
    saveCurrentScene();
    $("#library-search").value = "";
    renderLibrary();
    if (!$("#library-dialog").open) $("#library-dialog").showModal();
    try {
      const result = await api("/api/videos");
      state.videos = Array.isArray(result.videos) ? result.videos : state.videos;
      renderAssetSelect();
      await Promise.allSettled(state.videos.map(async (video) => {
        const result = await api(`/api/videos/${encodeURIComponent(video.id)}/edits`);
        state.savedEdits.set(video.id, Array.isArray(result.edits) ? result.edits : []);
      }));
      if ($("#library-dialog").open) renderLibrary();
    } catch (error) { toast(`Showing loaded videos. Could not refresh the library: ${error.message}`, true); }
  }

  function upsertVideo(video) {
    if (!video?.id) throw new Error("The server did not return a video.");
    const index = state.videos.findIndex((item) => item.id === video.id);
    if (index === -1) state.videos.unshift(video); else state.videos[index] = video;
    if (!state.conversations.has(video.id)) state.conversations.set(video.id, storedMessages(video.id));
  }

  async function selectVideo(video, loadEdits = true, preserveCurrentActivity = false) {
    const sequence = ++state.selectionSequence;
    const changed = state.video?.id !== video.id;
    if (changed) saveCurrentScene();
    const saved = storedScene(video.id);
    upsertVideo(video);
    state.video = video;
    renderAssetSelect();
    $("#empty-state").hidden = true;
    elements.video.hidden = false;
    const media = safeMedia(video.url);
    if (media && elements.video.src !== media) {
      elements.video.src = media;
      elements.video.poster = safeMedia(video.thumbnail);
      elements.video.load();
    }
    $("#source-badge").hidden = false;
    $("#source-badge").textContent = video.demo ? "SCRIPTED SAMPLE · ORIGINAL" : "ORIGINAL";
    $("#source-badge").classList.toggle("demo", Boolean(video.demo));
    $("#video-meta").textContent = `${video.width} × ${video.height} · ${Number(video.fps || 0).toFixed(Number(video.fps) % 1 ? 1 : 0)} fps`;
    $("#duration").textContent = time(video.duration);
    $("#playhead").max = String(video.duration || 1);
    $("#playhead").disabled = false;
    $("#range-start").max = $("#range-end").max = String(video.duration);
    if (changed) {
      state.presetSelection = null;
      // Import jobs already belong to the new video; ordinary asset changes start a fresh log.
      if (!preserveCurrentActivity) clearActivity();
      state.strategy = video.demo ? "anchors" : saved.strategy === "anchors" ? "anchors" : "source_video";
      elements.video.pause();
      $("#range-start").value = typeof saved.start === "string" ? saved.start : "";
      $("#range-end").value = typeof saved.end === "string" ? saved.end : "";
      $("#range-badge").hidden = true;
      elements.input.value = typeof saved.draft === "string" ? saved.draft.slice(0, 4000) : "";
      $("#output-panel").hidden = true;
      elements.output.pause();
      elements.output.removeAttribute("src");
      elements.output.load();
      setMode(saved.mode === "edit" ? "edit" : "ask");
      const restorePlayhead = () => {
        if (sequence !== state.selectionSequence) return;
        const position = Number(saved.playhead);
        if (Number.isFinite(position)) elements.video.currentTime = Math.max(0, Math.min(position, Math.max(0, video.duration - 0.05)));
        updatePlayhead();
      };
      if (elements.video.readyState >= 1) restorePlayhead();
      else elements.video.addEventListener("loadedmetadata", restorePlayhead, { once: true });
    }
    renderTimeline();
    renderAnalysis();
    renderMessages();
    updateControls();
    if (loadEdits) {
      try {
        const [result, history] = await Promise.all([
          api(`/api/videos/${encodeURIComponent(video.id)}/edits`),
          api(`/api/videos/${encodeURIComponent(video.id)}/conversation`).catch((error) => {
            toast(`Saved edits are available, but conversation history could not be loaded: ${error.message}`, true);
            return { messages: [] };
          }),
        ]);
        if (sequence !== state.selectionSequence) return;
        if (Array.isArray(history.messages) && history.messages.length) {
          const local = messages();
          const preserved = local.filter((item) => item.edit || item.mode === "edit" || item.error);
          state.conversations.set(video.id, [...history.messages, ...preserved]);
        }
        const edits = Array.isArray(result.edits) ? result.edits : [];
        state.savedEdits.set(video.id, edits);
        const latestOutput = edits.find((edit) => edit.output_url);
        if (latestOutput) renderOutput(latestOutput, false);
        const latestCompleted = edits.find((edit) => edit.status === "completed");
        if (Array.isArray(latestCompleted?.run_events) && latestCompleted.run_events.length) {
          const savedId = latestCompleted.job_id || `saved-${latestCompleted.id}`;
          if (flow.job?.id !== savedId || flow.saved) {
            recordEvents({ id: savedId, status: "completed", events: latestCompleted.run_events, workflow: latestCompleted.workflow, started_at: latestCompleted.run_started_at, completed_at: latestCompleted.run_completed_at }, { saved: true });
          }
        }
        for (const edit of [...edits].reverse()) {
          const existing = messages().find((message) => message.edit?.id === edit.id);
          if (existing) existing.edit = edit;
          else messages().push({ role: "assistant", text: edit.instruction, edit });
        }
        renderMessages();
      } catch (error) { toast(`Video loaded, but edit history could not be restored: ${error.message}`, true); }
    }
    saveCurrentScene();
    renderEnvironments();
  }

  function renderTimeline() {
    const frames = state.video?.frames || [];
    $("#timeline").replaceChildren(...frames.map((frame) => {
      const button = el("button", "frame-button");
      button.type = "button";
      button.dataset.timestamp = String(frame.timestamp);
      button.setAttribute("aria-label", `Seek to ${time(frame.timestamp, true)}`);
      button.title = `Seek to ${time(frame.timestamp, true)}`;
      const image = el("img");
      image.src = safeMedia(frame.url);
      image.alt = `Video frame at ${time(frame.timestamp, true)}`;
      image.loading = "lazy";
      button.append(image, el("span", "", time(frame.timestamp)));
      button.addEventListener("click", () => seek(frame.timestamp));
      return button;
    }));
    $("#timeline-note").textContent = `${frames.length} sampled frames · click any frame to explore`;
    updatePlayhead();
  }

  function seek(timestamp) {
    if (!state.video) return;
    const value = Math.max(0, Math.min(Number(timestamp) || 0, Number(state.video.duration) || 0));
    elements.video.currentTime = value;
    elements.video.pause();
    updatePlayhead();
    if (window.innerWidth <= 800) elements.video.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function updatePlayhead() {
    const current = Number(elements.video.currentTime) || 0;
    $("#current-time").textContent = time(current);
    $("#playhead").value = String(current);
    const frames = [...document.querySelectorAll(".frame-button")];
    let nearest = null;
    let distance = Infinity;
    frames.forEach((button) => {
      const d = Math.abs(Number(button.dataset.timestamp) - current);
      if (d < distance) { distance = d; nearest = button; }
    });
    frames.forEach((button) => button.classList.toggle("current", button === nearest));
  }

  function setRange(start, end) {
    $("#range-start").value = Number(start).toFixed(1);
    $("#range-end").value = Number(end).toFixed(1);
    updateRange();
  }

  function updateRange() {
    const start = $("#range-start").value;
    const end = $("#range-end").value;
    const valid = start !== "" && end !== "" && Number(end) > Number(start);
    $("#range-badge").hidden = !valid;
    $("#range-badge").textContent = valid ? `${time(start, true)} → ${time(end, true)}` : "";
    document.querySelectorAll(".frame-button").forEach((button) => button.classList.toggle("in-range", valid && Number(button.dataset.timestamp) >= Number(start) && Number(button.dataset.timestamp) <= Number(end)));
  }

  function renderAnalysis() {
    const analysis = state.video?.analysis;
    const summary = $("#analysis-summary");
    summary.replaceChildren();
    summary.hidden = !analysis;
    const label = analysis ? state.video.demo ? "Scripted sample analysis" : "Scene analysis ready" : canAnalyze() ? "Ready to analyze" : "Connect a vision model to analyze";
    $("#analysis-state").replaceChildren(el("span", "status-dot"), document.createTextNode(label));
    $("#analyze-button").replaceChildren(icon("spark"), document.createTextNode(analysis ? "Reanalyze" : "Analyze video"));
    if (!analysis) return;
    summary.append(el("p", "", asText(analysis.summary)));
    if (Array.isArray(analysis.events) && analysis.events.length) {
      const chips = el("div", "summary-events");
      for (const event of analysis.events) {
        const chip = el("button", "event-chip");
        chip.type = "button";
        chip.title = asText(event.description || event.label);
        chip.append(el("time", "", time(event.start)), document.createTextNode(asText(event.label)));
        chip.addEventListener("click", () => seek(event.start));
        chips.append(chip);
      }
      summary.append(chips);
    }
    if (analysis.limitations) summary.append(el("p", "analysis-limitations", asText(analysis.limitations)));
    if (analysis.research && typeof analysis.research === "object") {
      const details = el("details", "research-details");
      details.append(el("summary", "", "Creative research notes"));
      const notes = el("dl");
      for (const [key, value] of Object.entries(analysis.research)) {
        if (!value || (Array.isArray(value) && !value.length)) continue;
        const text = Array.isArray(value) ? value.map(asText).join("\n• ").replace(/^/, "• ") : asText(value);
        notes.append(el("dt", "", key.replaceAll("_", " ")), el("dd", "", text));
      }
      details.append(notes);
      summary.append(details);
    }
  }

  function appendMessage(message) {
    if (!state.video) return;
    messages().push(message);
    renderMessages();
  }

  function renderMessages() {
    const items = messages();
    $("#chat-empty").hidden = items.length > 0;
    elements.messages.replaceChildren(...items.map(renderMessage));
    requestAnimationFrame(() => { elements.conversation.scrollTop = elements.conversation.scrollHeight; });
    scheduleSceneSave();
  }

  function renderMessage(message) {
    const card = el("article", `message message-${message.role}${message.error ? " message-error" : ""}${message.pending ? " message-pending" : ""}`);
    const heading = el("div", "message-heading");
    const user = message.role === "user";
    heading.append(el("span", "message-avatar", user ? "YOU" : "v."), el("strong", "", user ? "You" : message.error ? "Something needs attention" : "Vidset"));
    if (message.model) heading.append(el("span", "", `${names[message.provider] || message.provider || ""} · ${message.model}`));
    else if (message.edit?.demo) heading.append(el("span", "", "Scripted demo"));
    card.append(heading);
    if (message.text) {
      const content = el("div", "message-text", message.text);
      if (message.pending) content.prepend(el("span", "spinner"));
      card.append(content);
    }
    if (Array.isArray(message.evidence) && message.evidence.length) {
      const evidence = el("div", "evidence-list");
      for (const frame of message.evidence) {
        const button = el("button", "evidence-card");
        button.type = "button";
        button.title = `Show evidence at ${time(frame.timestamp, true)}`;
        const image = el("img");
        image.src = safeMedia(frame.url);
        image.alt = asText(frame.caption || `Evidence at ${time(frame.timestamp, true)}`);
        image.loading = "lazy";
        button.append(image, el("time", "", time(frame.timestamp, true)), el("span", "", frame.caption || "View this moment"));
        button.addEventListener("click", () => seek(frame.timestamp));
        evidence.append(button);
      }
      card.append(evidence);
    }
    if (message.suggested_range) {
      const { start, end } = message.suggested_range;
      const button = el("button", "use-range-button", `Edit this moment · ${time(start, true)}–${time(end, true)} ↗`);
      button.type = "button";
      button.addEventListener("click", () => { setMode("edit"); setRange(start, end); seek(start); elements.input.focus(); });
      card.append(button);
    }
    if (message.limitations) card.append(el("p", "message-limitations", asText(message.limitations)));
    if (message.edit) card.append(renderPlan(message.edit));
    return card;
  }

  function renderPlan(edit) {
    const sourceEdit = isSourceEdit(edit);
    const card = el("div", "edit-plan");
    const header = el("div", "plan-header");
    header.append(el("span", "", edit.status === "completed" ? sourceEdit ? "Source video edit complete" : "Replacement complete" : "Your edit blueprint"), el("span", "mono", `${time(edit.start, true)}–${time(edit.end, true)}`));
    card.append(header);
    card.append(el("p", "plan-method", sourceEdit ? "SOURCE VIDEO EDIT · Original clip + instruction" : "ANCHOR GENERATION · First frame + last frame"));
    const anchors = el("div", "plan-anchors");
    for (const position of ["start", "end"]) {
      const anchor = edit.anchors?.[position];
      if (!anchor) continue;
      const figure = el("figure", "anchor-card");
      const button = el("button");
      button.type = "button";
      button.title = `Seek to ${position} boundary`;
      const image = el("img");
      image.src = safeMedia(anchor.url);
      image.alt = `Original ${position === "start" ? "first" : "last"} frame of replacement interval`;
      button.append(image);
      button.addEventListener("click", () => seek(anchor.timestamp ?? edit[position]));
      const caption = el("figcaption");
      caption.append(el("span", "", sourceEdit ? `BEFORE · ${position.toUpperCase()}` : position === "start" ? "FIRST FRAME" : "LAST FRAME"), el("span", "mono", time(anchor.timestamp ?? edit[position], true)));
      figure.append(button, caption);
      anchors.append(figure);
    }
    card.append(anchors);
    if (edit.edited_anchors) {
      const editedAnchors = el("div", "plan-anchors");
      for (const position of ["start", "end"]) {
        const anchor = edit.edited_anchors[position];
        if (!anchor?.url) continue;
        const figure = el("figure", "anchor-card");
        const image = el("img");
        image.src = safeMedia(anchor.url);
        image.alt = sourceEdit ? `Edited video evidence at the ${position} boundary` : `Edited ${position} anchor`;
        figure.append(image, el("figcaption", "", sourceEdit ? `AFTER · ${position.toUpperCase()}` : `EDITED ${position.toUpperCase()} FRAME`));
        editedAnchors.append(figure);
      }
      card.append(editedAnchors);
    }
    const body = el("div", "plan-details");
    if (sourceEdit) body.append(el("p", "plan-rationale", "The selected source clip guides the edit and its motion. Boundary frames provide before-and-after evidence. Original audio is kept when present."));
    if (edit.rationale) body.append(el("p", "plan-rationale", asText(edit.rationale)));
    appendEditNotes(body, edit);
    const details = el("details");
    details.append(el("summary", "", "See the generation prompt"), el("p", "", asText(edit.prompt)));
    body.append(details);
    const button = el("button", "button button-lime generate-button");
    button.type = "button";
    const completed = edit.status === "completed";
    const generating = edit.status === "generating";
    button.dataset.finished = String(completed || generating);
    button.disabled = state.busy || completed || generating;
    button.append(icon(completed ? "check" : "spark"), document.createTextNode(completed ? "Replacement ready" : generating ? "Generation already in progress" : edit.demo ? "Generate sample replacement" : sourceEdit ? "Generate source video edit" : "Generate replacement"));
    button.addEventListener("click", () => generateEdit(edit));
    body.append(button, el("p", "generation-notice", edit.demo ? "Scripted graphics change · no AI calls or charges" : completed ? "Completed edit. Expand the saved run for actual provider details." : sourceEdit ? `Runs paid ${names[sourceProvider()]} video editing with your local API credentials. Your provider usage rates apply.` : "Runs paid OpenAI image edits + fal.ai video generation. Your provider usage rates apply."));
    if (edit.status === "failed") body.append(el("p", "generation-status", "The last generation failed. You can retry with the same plan."));
    if (generating) body.append(el("p", "generation-status", "This generation is still running on the server. Reload this page to check its result; no duplicate generation will be started."));
    if (completed && edit.output_url) {
      const view = el("button", "button button-text", "View your new cut ↗");
      view.type = "button";
      view.addEventListener("click", () => renderOutput(edit, true));
      body.append(view);
    }
    card.append(body);
    return card;
  }

  function appendEditNotes(container, edit) {
    for (const [key, label] of [["provenance", "Run provenance"], ["review", "Result review"]]) {
      if (!edit[key]) continue;
      const note = el("div", "edit-annotation");
      note.append(el("strong", "", label), el("p", "", asText(edit[key])));
      container.append(note);
    }
  }

  function renderOutput(edit, scroll = true) {
    const url = safeMedia(edit.output_url);
    const versions = (state.savedEdits.get(state.video?.id) || []).filter((item) => item.output_url && item.status === "completed");
    if (!versions.some((item) => item.id === edit.id)) versions.unshift(edit);
    $("#output-version").replaceChildren(...versions.map((item) => {
      const option = el("option", "", presetName(item.preset_id) || asText(item.instruction).slice(0, 85) || "Saved edit");
      option.value = item.id; return option;
    }));
    $("#output-version").value = edit.id;
    if (!url) return;
    $("#output-panel").hidden = false;
    elements.output.poster = safeMedia(edit.edited_anchors?.start?.url);
    if (elements.output.src !== url) {
      elements.output.addEventListener("loadedmetadata", () => {
        if (elements.output.src === url) {
          const duration = Number.isFinite(elements.output.duration) ? elements.output.duration : Number(edit.start) || 0;
          elements.output.currentTime = Math.max(0, Math.min(Number(edit.start) || 0, duration));
        }
      }, { once: true });
      elements.output.src = url;
      elements.output.load();
    }
    $("#download-output").href = url;
    $("#download-output").download = `vidset-${edit.id}.mp4`;
    $("#output-description").textContent = `${edit.demo ? "SCRIPTED DEMO · " : isSourceEdit(edit) ? "SOURCE VIDEO EDIT · " : "ANCHOR GENERATION · "}${time(edit.start, true)}–${time(edit.end, true)} replaced. ${edit.demo ? "This sample uses a deterministic graphics change, with no generative model calls." : "Original audio is preserved when present. Review the subject, movement, and boundaries for continuity."}`;
    const evidence = $("#output-evidence");
    evidence.replaceChildren();
    if (edit.anchors && edit.edited_anchors) {
      const comparison = el("details", "output-comparison");
      comparison.open = isSourceEdit(edit);
      comparison.append(el("summary", "", "Compare before & after"));
      const grid = el("div", "comparison-grid");
      for (const position of ["start", "end"]) {
        const timestamp = edit.anchors[position]?.timestamp ?? edit[position];
        for (const version of ["before", "after"]) {
          const anchor = version === "before" ? edit.anchors[position] : edit.edited_anchors[position];
          if (!safeMedia(anchor?.url)) continue;
          const figure = el("figure", "comparison-frame");
          const button = el("button");
          button.type = "button";
          button.title = `Show ${version === "before" ? "original" : "edited"} video at ${time(timestamp, true)}`;
          const image = el("img");
          image.src = safeMedia(anchor.url);
          image.alt = `${version === "before" ? "Original" : "Edited"} frame at the ${position} of the replacement`;
          button.append(image);
          button.addEventListener("click", () => {
            if (version === "before") seek(timestamp);
            else if (elements.output.readyState >= 1) {
              elements.output.pause();
              elements.output.currentTime = Math.max(0, Math.min(Number(timestamp) || 0, elements.output.duration));
            }
          });
          figure.append(button, el("figcaption", "", `${version.toUpperCase()} · ${position.toUpperCase()} · ${time(timestamp, true)}`));
          grid.append(figure);
        }
      }
      comparison.append(grid);
      evidence.append(comparison);
    }
    const notes = $("#output-notes");
    notes.replaceChildren();
    appendEditNotes(notes, edit);
    if (edit.prompt) {
      const prompt = el("details", "output-prompt");
      prompt.append(el("summary", "", "Generation prompt"), el("p", "", asText(edit.prompt)));
      notes.append(prompt);
    }
    if (scroll) $("#output-panel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function clearActivity() {
    clearInterval(flow.tick);
    flow.tick = null;
    for (const timer of flow.collapseTimers.values()) clearTimeout(timer);
    flow.collapseTimers.clear();
    flow.open.clear();
    flow.previous.clear();
    flow.job = flow.view = null;
    flow.saved = false;
    flow.signature = "";
    state.events = [];
    state.seenEvents.clear();
    const empty = el("div", "activity-empty");
    empty.id = "activity-empty";
    empty.append(el("p", "", "No events recorded for this run yet."));
    $("#activity-feed").replaceChildren(empty);
    $("#activity-count").textContent = "00";
    $("#activity-log-label").textContent = "YOUR WORKFLOW";
    $(".activity-intro").textContent = "Follow the work, one observable step at a time.";
    $("#activity-status").textContent = "Ready for this scene";
    $("#workflow-title").textContent = "A little clarity at every step.";
    $("#workflow-steps").replaceChildren();
    $("#workflow-progress").hidden = true;
    $("#workflow-notice").hidden = true;
    $("#workflow-elapsed").textContent = "";
    $("#workflow-empty").hidden = false;
    $("#workflow-empty h4").textContent = "Your next move, made visible.";
    $("#workflow-empty p").textContent = "Each phase appears with its model, evidence, and actual result.";
    $("#raw-events-details").open = false;
  }

  const elapsedLabel = (milliseconds) => {
    if (!Number.isFinite(milliseconds)) return "";
    const seconds = Math.max(0, Math.floor(milliseconds / 1000));
    return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
  };

  function updateWorkflow(job, { saved = false } = {}) {
    flow.job = job;
    flow.saved = saved;
    const view = window.VideoWorkflow.derive(job, { saved, now: Date.now() });
    flow.view = view;
    $("#workflow-title").textContent = view.title;
    $("#activity-log-label").textContent = saved ? "SAVED RUN" : view.status === "completed" ? "COMPLETED RUN" : view.status === "failed" ? "RUN STOPPED" : "LIVE WORKFLOW";
    $(".activity-intro").textContent = saved ? "Recorded steps from this completed edit. No new model calls." : "Follow the work, one observable step at a time.";
    $("#workflow-progress").hidden = !view.steps.length;
    $("#workflow-count").textContent = view.progressLabel;
    $("#workflow-summary").textContent = [view.counts.skipped ? `${view.counts.skipped} skipped` : "", view.counts.failed ? `${view.counts.failed} stopped` : ""].filter(Boolean).join(" · ");
    $("#workflow-elapsed").textContent = elapsedLabel(view.elapsedMs);
    $("#workflow-elapsed").title = "Elapsed from the recorded run start";
    const notice = view.stale ? "Connection interrupted. This is the last recorded state; no further completion is assumed." : view.error || (saved && view.derived ? "This legacy run shows only the stages recorded in its event log." : "");
    $("#workflow-notice").textContent = notice;
    $("#workflow-notice").hidden = !notice;
    $("#workflow-notice").classList.toggle("error", view.status === "failed");
    $("#workflow-empty").hidden = view.steps.length > 0;
    if (!view.steps.length) {
      $("#workflow-empty h4").textContent = view.status === "preparing" ? "Preparing the request…" : view.status === "failed" ? "The request could not start." : "Waiting for recorded steps…";
      $("#workflow-empty p").textContent = view.status === "preparing" ? "Sending your request to the local server. No model step has started yet." : view.status === "failed" ? "No completed model calls are inferred. Review the error above and try again." : "The server will report each phase as it happens.";
    }
    const active = view.steps.find((step) => step.status === "running");
    $("#activity-status").textContent = saved ? "Saved run · no new model calls" : view.stale ? "Connection lost · last known state" : view.status === "failed" ? "Run stopped · unstarted steps remain pending" : view.status === "completed" ? "Run complete · ready for the next step" : view.status === "preparing" ? "Preparing the local request" : active ? active.title : "Waiting for the next recorded step";
    const signature = JSON.stringify([view.id, view.status, saved, view.steps.map((step) => [step.id, step.status, step.events.length, step.latestEvent?.message, step.provider, step.model])]);
    if (signature !== flow.signature) {
      flow.signature = signature;
      $("#workflow-steps").replaceChildren(...view.steps.map(renderWorkflowStep));
    }
    for (const step of view.steps) {
      const timer = document.getElementById(`workflow-time-${step.id}`);
      if (timer) timer.textContent = elapsedLabel(step.elapsedMs);
    }
    clearInterval(flow.tick);
    flow.tick = null;
    if (!saved && !view.stale && ["running", "queued"].includes(view.status)) {
      flow.tick = setInterval(() => { if (flow.job) updateWorkflow(flow.job, { saved: flow.saved }); }, 1000);
    }
  }

  function renderWorkflowStep(step) {
    const previous = flow.previous.get(step.id);
    const changed = previous !== step.status;
    if (changed && ["running", "failed"].includes(step.status)) flow.open.set(step.id, true);
    if (!flow.open.has(step.id)) flow.open.set(step.id, false);
    flow.previous.set(step.id, step.status);
    const item = el("li", `workflow-step is-${step.status}`);
    item.dataset.stepId = step.id;
    const marker = el("span", "workflow-marker");
    marker.setAttribute("aria-hidden", "true");
    if (step.status === "completed") marker.append(icon("check"));
    else if (step.status === "running") marker.append(el("span", "step-spinner"));
    else marker.textContent = step.status === "failed" ? "!" : step.status === "skipped" ? "–" : "";
    const details = el("details", "workflow-step-card");
    details.open = Boolean(flow.open.get(step.id));
    details.addEventListener("toggle", () => { flow.open.set(step.id, details.open); });
    const summary = el("summary");
    const heading = el("span", "workflow-step-heading");
    heading.append(el("strong", "", step.title), el("span", "workflow-step-state", step.status === "pending" ? "Not started" : step.status === "running" ? "In progress" : step.status === "failed" ? "Stopped" : step.status === "completed" ? "Complete" : "Skipped"));
    const timer = el("time", "workflow-step-time", elapsedLabel(step.elapsedMs));
    timer.id = `workflow-time-${step.id}`;
    timer.title = "Elapsed from recorded step timestamps or the provider’s reported duration";
    summary.append(heading, timer, el("span", "workflow-chevron", "⌄"));
    details.append(summary);
    const content = el("div", "workflow-step-content");
    const identity = [names[step.provider] || step.provider, step.model].filter(Boolean).join(" / ");
    if (identity) content.append(el("p", "workflow-step-model", `${step.observed ? "" : "Planned: "}${identity}`));
    content.append(el("p", "workflow-step-message", step.reason || step.latestEvent?.message || "This step has not started. No provider call is recorded yet."));
    if (step.artifacts.length) {
      const artifacts = el("div", "workflow-artifacts");
      for (const artifact of step.artifacts.slice(-6)) {
        const url = safeMedia(artifact.url);
        if (!url) continue;
        const link = el("a", "workflow-artifact");
        link.href = url; link.target = "_blank"; link.rel = "noopener";
        const label = asText(artifact.label || "Evidence");
        if (/\.(mp4|webm|mov)(?:\?|$)/i.test(url)) link.append(icon("film", false));
        else { const image = el("img"); image.src = url; image.alt = label; image.loading = "lazy"; link.append(image); }
        link.append(el("span", "", label));
        artifacts.append(link);
      }
      content.append(artifacts);
    }
    if (step.events.length > 1 && step.status !== "running") {
      const history = el("ol", "workflow-observations");
      for (const event of step.events) {
        const line = el("li");
        line.append(el("span", "", event.status === "running" ? "Started" : event.status === "completed" ? "Completed" : event.status === "failed" ? "Failed" : "Update"), document.createTextNode(asText(event.message)));
        history.append(line);
      }
      content.append(history);
    }
    details.append(content);
    item.append(marker, details);
    if (changed && previous === "running" && step.status === "completed") {
      clearTimeout(flow.collapseTimers.get(step.id));
      flow.collapseTimers.set(step.id, setTimeout(() => {
        if (flow.previous.get(step.id) === "completed") {
          const current = [...document.querySelectorAll(".workflow-step")].find((node) => node.dataset.stepId === step.id);
          if (current) current.querySelector("details").open = false;
          flow.open.set(step.id, false);
        }
        flow.collapseTimers.delete(step.id);
      }, 320));
    }
    return item;
  }

  function recordEvents(job, options = {}) {
    renderRouting(job.routing || job.workflow?.routing, Boolean(job.routing || job.workflow?.routing));
    if (flow.job && flow.job.id !== job.id && flow.job.status !== "preparing") clearActivity();
    const newEvents = [];
    for (let index = 0; index < (job.events || []).length; index++) {
      const event = job.events[index];
      const key = `${job.id}:${event.id ?? index}`;
      if (state.seenEvents.has(key)) continue;
      state.seenEvents.add(key);
      state.events.push({ ...event, jobId: job.id });
      newEvents.push({ ...event, jobId: job.id });
    }
    if (newEvents.length) $("#activity-empty")?.remove();
    const feed = $("#activity-feed");
    const wasNearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    for (const event of newEvents) {
      if (["completed", "failed", "skipped"].includes(event.status)) {
        const key = [event.jobId, event.stage, event.provider, event.model].join("::");
        for (const prior of feed.querySelectorAll(".activity-entry.running")) {
          if (prior.dataset.stageKey === key) {
            prior.classList.add("settled");
            prior.querySelector(".event-status").textContent = "STARTED";
          }
        }
      }
      feed.append(renderEvent(event));
    }
    $("#activity-count").textContent = String(state.events.length).padStart(2, "0");
    if (["completed", "failed"].includes(job.status) || options.saved) {
      for (const entry of feed.querySelectorAll(".activity-entry.running")) {
        entry.classList.add("settled");
        entry.querySelector(".event-status").textContent = "STARTED";
      }
    }
    if (wasNearBottom) feed.scrollTop = feed.scrollHeight;
    updateWorkflow(job, options);
  }

  function renderRouting(payload, actual = false) {
    const data = payload || state.routing;
    if (!data) return;
    if (state.video?.demo) { $("#routing-card").hidden = true; state.routingSignature = ""; return; }
    const selectedRole = state.mode === "edit" ? (state.strategy === "anchors" ? "anchors" : "source_video") : "vision";
    const decisions = actual ? data.decisions || [] : (data.decisions || []).filter(item => item.role === selectedRole);
    const signature = JSON.stringify([decisions, data.review, actual]);
    if (state.routingSignature === signature) return;
    state.routingSignature = signature;
    $("#routing-card").hidden = !decisions.length;
    const review = $("#routing-review");
    review.textContent = data.review?.label || "Prelint · pending";
    review.classList.toggle("reviewed", data.review?.state === "reviewed");
    review.title = data.review?.state === "reviewed" ? "Routing policy reviewed by Prelint. Selection runs locally." : "Local routing policy. Prelint review is not yet verified for this revision.";
    $("#routing-decisions").replaceChildren(...decisions.map(decision => {
      const row = el("div", "routing-decision");
      const choice = el("div", "routing-choice");
      choice.append(el("span", "routing-role", decision.title), el("strong", "", decision.chosen?.label || "Key needed"));
      if (!actual) choice.append(el("span", "routing-next", "Next call"));
      row.append(choice, el("p", "routing-reason", decision.reason));
      const options = el("details", "routing-options");
      options.append(el("summary", "", "Model options"));
      const list = el("div", "routing-candidates");
      for (const candidate of decision.candidates || []) {
        const option = el("span", `routing-candidate ${candidate.status}`);
        option.title = `${candidate.provider} / ${candidate.model} · ${candidate.reason}`;
        option.append(el("span", "", candidate.label), el("small", "", candidate.reason));
        list.append(option);
      }
      options.append(list); row.append(options); return row;
    }));
  }

  function renderEvent(event) {
    const status = ["running", "completed", "failed", "info"].includes(event.status) ? event.status : "info";
    const card = el("article", `activity-entry ${status}`);
    card.dataset.stageKey = [event.jobId, event.stage, event.provider, event.model].join("::");
    const header = el("div", "activity-entry-title");
    header.append(el("strong", "", asText(event.stage).replaceAll("_", " ")));
    if (event.elapsed_ms !== undefined) header.append(el("time", "", `${(Number(event.elapsed_ms) / 1000).toFixed(1)}s`));
    else if (event.time) {
      const date = new Date(event.time);
      if (!Number.isNaN(date.getTime())) header.append(el("time", "", date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
    }
    card.append(header, el("span", "activity-entry-model", [names[event.provider] || event.provider, event.model].filter(Boolean).join(" / ")), el("p", "", asText(event.message)), el("span", "event-status", status.toUpperCase()));
    if (Array.isArray(event.artifacts) && event.artifacts.length) {
      const artifacts = el("div", "activity-artifacts");
      for (const artifact of event.artifacts) {
        const media = safeMedia(artifact.url);
        if (!media) continue;
        const isVideo = /\.(mp4|webm|mov)(?:\?|$)/i.test(media);
        const link = el("a", "artifact-link");
        link.href = media;
        link.target = "_blank";
        link.rel = "noopener";
        link.title = asText(artifact.label || "Open artifact");
        if (isVideo) link.append(icon("film", false));
        else {
          const img = el("img");
          img.src = media;
          img.alt = asText(artifact.label || "Frame artifact");
          img.loading = "lazy";
          link.append(img);
        }
        link.append(el("span", "", `${artifact.label || "Artifact"}${artifact.timestamp !== undefined ? ` · ${time(artifact.timestamp)}` : ""}`));
        artifacts.append(link);
      }
      card.append(artifacts);
    }
    return card;
  }

  async function runJob(path, body, label) {
    if (state.busy) throw new Error("Wait for the current step to finish.");
    clearActivity();
    updateWorkflow({ id: "preparing", status: "preparing", events: [], workflow: { title: label, steps: [] } });
    setBusy(true, label);
    try {
      const submitted = await api(path, { method: "POST", body });
      if (!submitted.job_id) throw new Error("The server did not return a job ID.");
      let failures = 0;
      const started = Date.now();
      while (true) {
        let job;
        try {
          job = await api(`/api/jobs/${encodeURIComponent(submitted.job_id)}`);
          failures = 0;
        } catch (error) {
          failures++;
          if (failures >= 8) throw new Error(`${error.message} Job ${submitted.job_id} may still be running on the server.`);
          $("#activity-status").textContent = "Connection interrupted · reconnecting…";
          await wait(Math.min(failures * 1000, 5000));
          continue;
        }
        recordEvents(job);
        if (job.status === "completed") return job.result || {};
        if (job.status === "failed") throw new Error(asText(job.error || "This step failed. Check the activity log for details."));
        const latest = job.events?.at(-1);
        $("#activity-status").textContent = latest?.stage ? asText(latest.stage).replaceAll("_", " ") : label;
        if (Date.now() - started > 30 * 60 * 1000) throw new Error(`This step is taking longer than 30 minutes. Job ${submitted.job_id} may still be running; no replacement is shown until completion.`);
        await wait(1000);
      }
    } catch (error) {
      if (flow.job?.status === "preparing") updateWorkflow({ ...flow.job, status: "failed", error: error.message });
      else if (flow.job && !["completed", "failed"].includes(flow.job.status)) updateWorkflow({ ...flow.job, stale: true });
      throw error;
    } finally { setBusy(false); }
  }

  async function loadFile(file) {
    if (!file || state.busy) return;
    saveCurrentScene();
    const maxMb = Number(state.config?.limits?.max_mb || 128);
    if (file.size > maxMb * 1024 * 1024) { toast(`Choose a video smaller than ${maxMb} MB.`, true); return; }
    if (file.type && !file.type.startsWith("video/") && !/\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(file.name)) { toast("Choose a video file, such as MP4, MOV, or WebM.", true); return; }
    $("#media-loading-text").textContent = "Preparing your footage…";
    $("#media-loading").hidden = false;
    clearActivity();
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await runJob("/api/videos", form, "Importing and sampling your video");
      await selectVideo(result.video, false, true);
      toast("New video added to your library. Your previous videos and edits are still saved.");
      $("#media-loading").hidden = true;
      if (canAnalyze()) await analyze();
      else toast("Video ready. Connect a vision model in Models & keys to analyze it.");
    } catch (error) { toast(error.message, true); }
    finally { $("#media-loading").hidden = true; $("#file-input").value = ""; }
  }

  async function loadDemo() {
    if (state.busy) return;
    $("#media-loading-text").textContent = "Creating the sample scene…";
    $("#media-loading").hidden = false;
    clearActivity();
    try {
      const result = await runJob("/api/demo", undefined, "Creating a local scripted sample");
      await selectVideo(result.video, false, true);
      $("#media-loading").hidden = true;
      if (!state.video.analysis) await analyze();
      toast("Sample loaded. This demo is scripted and makes no AI calls.");
    } catch (error) { toast(error.message, true); }
    finally { $("#media-loading").hidden = true; }
  }

  async function analyze() {
    if (!state.video || state.busy) return;
    if (!canAnalyze()) { openSettings(); toast("Add a key for your selected vision provider to analyze this video."); return; }
    const id = state.video.id;
    try {
      $("#analysis-state").replaceChildren(el("span", "status-dot"), document.createTextNode("Looking at your scene…"));
      const result = await runJob(`/api/videos/${encodeURIComponent(id)}/analyze`, undefined, "Analyzing the scene");
      if (result.video) await selectVideo(result.video, false);
      else {
        const video = await api(`/api/videos/${encodeURIComponent(id)}`);
        await selectVideo(video, false);
      }
    } catch (error) { renderAnalysis(); toast(error.message, true); }
  }

  async function submitMessage(event) {
    event.preventDefault();
    const message = elements.input.value.trim();
    if (!message || !state.video || state.busy) return;
    const mode = state.mode;
    const videoId = state.video.id;
    const body = { message };
    if (mode === "edit") {
      const sourceEdit = currentStrategy() === "source_video";
      const minDuration = sourceEdit ? 4 : 1;
      const maxDuration = sourceEdit ? 30 : 10;
      const start = $("#range-start").value;
      const end = $("#range-end").value;
      if (message.length < 3) { toast("Describe the change in a little more detail.", true); return; }
      if (state.video.duration < minDuration) { toast(sourceEdit ? "Source video editing needs at least 4 seconds. Choose the anchor workflow for a shorter clip." : "Anchor generation needs at least 1 second of footage.", true); return; }
      if ((start === "") !== (end === "")) { toast("Enter both start and end times, or leave both blank for AI to locate the moment.", true); return; }
      if (start !== "" && (!Number.isFinite(Number(start)) || Number(start) < 0 || Number(start) >= state.video.duration)) { toast("The start time must fall inside this video.", true); return; }
      if (end !== "" && (!Number.isFinite(Number(end)) || Number(end) <= 0 || Number(end) > state.video.duration)) { toast("The end time must fall inside this video.", true); return; }
      if (start !== "" && end !== "" && (Number(end) - Number(start) < minDuration || Number(end) - Number(start) > maxDuration)) { toast(`Choose an interval between ${minDuration} and ${maxDuration} seconds for this workflow.`, true); return; }
      if (start !== "") body.start = Number(start);
      if (end !== "") body.end = Number(end);
      body.instruction = message;
      body.strategy = currentStrategy();
      if (state.presetSelection?.instruction === message) body.preset_id = state.presetSelection.id;
      delete body.message;
    } else {
      body.history = messages().filter((item) => item.text && !item.edit && !item.pending && !item.error && item.mode !== "edit").slice(-12).map((item) => ({ role: item.role, content: item.text }));
    }
    const pending = { role: "assistant", text: mode === "edit" ? "Finding the moment and preparing its boundary frames…" : "Looking for the answer in your footage…", pending: true };
    appendMessage({ role: "user", text: message, mode });
    appendMessage(pending);
    elements.input.value = "";
    try {
      const path = `/api/videos/${encodeURIComponent(videoId)}/${mode === "edit" ? "edits/plan" : "chat"}`;
      const result = await runJob(path, body, mode === "edit" ? "Planning the replacement" : "Finding evidence in the video");
      pending.pending = false;
      if (mode === "edit") {
        if (!result.edit) throw new Error("The server did not return an edit plan.");
        pending.text = isSourceEdit(result.edit) ? "Here’s the source moment to edit. Review the original boundary evidence and prompt, then generate when you’re ready. The source clip will guide the motion." : "Here’s the moment to change. Review the original boundary frames and generation prompt, then generate when you’re ready.";
        pending.edit = result.edit;
        const saved = state.savedEdits.get(videoId) || [];
        state.savedEdits.set(videoId, [result.edit, ...saved.filter((item) => item.id !== result.edit.id)]);
        state.presetSelection = null;
        setRange(result.edit.start, result.edit.end);
        seek(result.edit.start);
      } else {
        if (!result.answer) throw new Error("The server did not return an answer.");
        Object.assign(pending, { text: asText(result.answer), evidence: result.evidence, suggested_range: result.suggested_range, model: result.model, provider: result.provider, limitations: result.limitations });
      }
      renderMessages();
      // Chat and edit planning can analyze an unindexed clip before answering.
      const fresh = await api(`/api/videos/${encodeURIComponent(videoId)}`).catch(() => null);
      if (fresh && state.video?.id === videoId) { upsertVideo(fresh); state.video = fresh; renderAnalysis(); }
    } catch (error) {
      pending.pending = false;
      pending.error = true;
      pending.text = error.message;
      renderMessages();
    } finally { updateControls(); renderEnvironments(); elements.input.focus({ preventScroll: true }); }
  }

  async function generateEdit(edit) {
    if (state.busy || !state.video || ["completed", "generating"].includes(edit.status)) return;
    const sourceEdit = isSourceEdit(edit);
    const provider = sourceProvider();
    if (!edit.demo && (sourceEdit ? !isConfigured(provider) : !isConfigured("openai") || !isConfigured("fal"))) {
      openSettings();
      toast(sourceEdit ? `Source editing needs your ${provider === "fal" ? "FAL_KEY" : "Higgsfield API ID:secret"}. Your plan is saved.` : "Anchor generation needs both an OpenAI key and a fal.ai key. Your plan is saved.");
      return;
    }
    state.strategy = sourceEdit ? "source_video" : "anchors";
    renderStrategy();
    setRange(edit.start, edit.end);
    try {
      const result = await runJob(`/api/videos/${encodeURIComponent(edit.video_id || state.video.id)}/edits/${encodeURIComponent(edit.id)}/generate`, undefined, edit.demo ? "Creating the scripted replacement" : sourceEdit ? `Editing the source clip with ${names[provider]}` : "Editing anchors and generating the replacement");
      if (!result.edit?.output_url) throw new Error("Generation completed without a playable output.");
      Object.assign(edit, result.edit);
      renderMessages();
      renderOutput(edit);
      toast(edit.demo ? "Sample replacement ready. No AI models were used." : "Your replacement is ready to review.");
    } catch (error) {
      edit.status = "failed";
      appendMessage({ role: "assistant", text: error.message, error: true });
      renderMessages();
    } finally { updateControls(); refreshEnvironments(); }
  }

  function openSettings() {
    if (state.busy) return;
    const config = state.config;
    $("#key-fields").replaceChildren(...credentialProviders.map((provider) => {
      const field = el("div", `key-field${provider === "higgsfield" ? " wide-field" : ""}`);
      const label = el("label", "", provider === "higgsfield" ? "Higgsfield · API ID:secret" : provider === "fal" ? "fal.ai · FAL_KEY" : names[provider]);
      label.htmlFor = `key-${provider}`;
      const configured = isConfigured(provider);
      label.append(el("span", `key-status${configured ? " configured" : ""}`, configured ? "● CONNECTED" : "NOT CONNECTED"));
      const input = el("input");
      input.id = `key-${provider}`;
      input.type = "password";
      input.name = `key_${provider}`;
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = configured ? "Paste new credentials to replace" : provider === "higgsfield" ? "API_ID:API_SECRET" : provider === "fal" ? "Paste FAL_KEY" : "Paste API key";
      field.append(label, input);
      return field;
    }));
    $("#video-edit-model").value = config?.models?.video_edit_model || "bytedance/seedance-2.5/video-edit";
    $("#fal-video-edit-model").value = config?.models?.fal_video_edit_model || "bytedance/seedance-2.5/reference-to-video";
    $("#video-edit-provider").value = sourceProvider();
    if (config?.models) {
      for (const [key, value] of Object.entries(config.models)) {
        const field = $(`#settings-form [name="${key}"]`);
        if (field) field.value = value;
      }
    }
    updateSourceModelFields();
    $("#settings-error").hidden = true;
    $("#settings-dialog").showModal();
  }

  async function saveSettings(event) {
    event.preventDefault();
    const keys = {};
    for (const provider of credentialProviders) {
      const value = $(`#key-${provider}`).value.trim();
      if (value) keys[provider] = value;
    }
    const models = {};
    for (const field of ["vision_provider", "vision_model", "chat_provider", "chat_model", "image_model", "video_model", "video_edit_model", "video_edit_provider", "fal_video_edit_model"]) {
      const input = $(`#settings-form [name="${field}"]`);
      const value = input.value.trim();
      if (value !== state.config?.models?.[field]) models[field] = value;
    }
    $("#save-settings").disabled = true;
    $("#settings-error").hidden = true;
    try {
      state.config = await api("/api/config", { method: "POST", body: { keys, models } });
      state.routing = await api("/api/routing");
      renderRouting();
      $("#settings-dialog").close();
      for (const field of document.querySelectorAll("#key-fields input")) field.value = "";
      renderStrategy();
      renderMessages();
      if (state.video) renderAnalysis();
      toast("Connections saved for this server session.");
    } catch (error) { $("#settings-error").textContent = error.message; $("#settings-error").hidden = false; }
    finally { $("#save-settings").disabled = false; }
  }

  // Explicit UI gestures are the only way to start paid replacement generation.
  $("#upload-button").addEventListener("click", () => $("#file-input").click());
  $("#new-upload").addEventListener("click", () => $("#file-input").click());
  $("#library-button").addEventListener("click", openLibrary);
  $("#browse-library").addEventListener("click", openLibrary);
  $("#close-library").addEventListener("click", () => $("#library-dialog").close());
  $("#library-search").addEventListener("input", renderLibrary);
  $("#library-upload").addEventListener("click", () => {
    if (state.busy) return;
    saveCurrentScene();
    $("#library-dialog").close();
    $("#file-input").click();
  });
  $("#file-input").addEventListener("change", (event) => loadFile(event.target.files[0]));
  $("#output-version").addEventListener("change", (event) => {
    const edit = (state.savedEdits.get(state.video?.id) || []).find((item) => item.id === event.target.value);
    if (edit) renderOutput(edit, false);
  });
  $("#demo-button").addEventListener("click", loadDemo);
  $("#analyze-button").addEventListener("click", analyze);
  $("#settings-button").addEventListener("click", openSettings);
  $("#close-settings").addEventListener("click", () => $("#settings-dialog").close());
  $("#settings-dialog").addEventListener("close", () => { for (const field of document.querySelectorAll("#key-fields input")) field.value = ""; });
  $("#settings-form").addEventListener("submit", saveSettings);
  $("#chat-form").addEventListener("submit", submitMessage);
  elements.input.addEventListener("input", () => { updateControls(); scheduleSceneSave(); });
  elements.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#chat-form").requestSubmit(); }
  });
  for (const mode of ["ask", "edit"]) {
    $(`#${mode}-tab`).addEventListener("click", () => { setMode(mode); scheduleSceneSave(); });
    $(`#${mode}-tab`).addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); const next = state.mode === "ask" ? "edit" : "ask"; setMode(next); $(`#${next}-tab`).focus(); }
    });
  }
  $("#suggestions").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-prompt]");
    if (!button) return;
    if (!state.video) { toast("Start by uploading a video or exploring the sample."); return; }
    if (state.busy) return;
    elements.input.value = button.dataset.prompt;
    updateControls();
    elements.input.focus();
  });
  $("#asset-select").addEventListener("change", async (event) => {
    if (state.busy) return;
    const video = state.videos.find((item) => item.id === event.target.value);
    if (video) await selectVideo(video);
  });
  elements.video.addEventListener("timeupdate", updatePlayhead);
  elements.video.addEventListener("loadedmetadata", updatePlayhead);
  elements.video.addEventListener("pause", scheduleSceneSave);
  elements.video.addEventListener("seeked", scheduleSceneSave);
  window.addEventListener("pagehide", saveCurrentScene);
  $("#playhead").addEventListener("input", (event) => { if (state.video) elements.video.currentTime = Number(event.target.value); });
  $("#range-start").addEventListener("input", () => { updateRange(); scheduleSceneSave(); });
  $("#range-end").addEventListener("input", () => { updateRange(); scheduleSceneSave(); });
  $("#edit-strategy").addEventListener("change", (event) => {
    state.strategy = event.target.value === "anchors" ? "anchors" : "source_video";
    renderStrategy();
    if (!state.busy) renderRouting();
    updateControls();
    scheduleSceneSave();
  });
  const dropZone = $("#drop-zone");
  dropZone.addEventListener("dragenter", (event) => { event.preventDefault(); state.dragDepth++; if (!state.busy) $("#drop-overlay").hidden = false; });
  dropZone.addEventListener("dragover", (event) => { event.preventDefault(); event.dataTransfer.dropEffect = state.busy ? "none" : "copy"; });
  dropZone.addEventListener("dragleave", (event) => { event.preventDefault(); state.dragDepth--; if (state.dragDepth <= 0) { state.dragDepth = 0; $("#drop-overlay").hidden = true; } });
  dropZone.addEventListener("drop", (event) => { event.preventDefault(); state.dragDepth = 0; $("#drop-overlay").hidden = true; if (!state.busy) loadFile(event.dataTransfer.files[0]); });
  window.addEventListener("dragover", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
  window.addEventListener("drop", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });

  const defaultModels = { gemini: "gemini-2.5-flash", openai: "gpt-4.1-mini", anthropic: "claude-sonnet-4-20250514" };
  $("#vision-provider").addEventListener("change", (event) => { $("#vision-model").value = event.target.value === "fal" ? "google/gemini-2.5-flash" : defaultModels[event.target.value]; });
  $("#chat-provider").addEventListener("change", (event) => { $("#chat-model").value = event.target.value === "fal" ? "openai/gpt-4.1-mini" : defaultModels[event.target.value]; });
  function updateSourceModelFields() {
    const fal = $("#video-edit-provider").value === "fal";
    $("#source-fal-model-field").hidden = !fal;
    $("#source-higgsfield-model-field").hidden = fal;
  }
  $("#video-edit-provider").addEventListener("change", updateSourceModelFields);
  $("#use-fal-route").addEventListener("click", () => {
    $("#vision-provider").value = "fal";
    $("#vision-model").value = "google/gemini-2.5-flash";
    $("#chat-provider").value = "fal";
    $("#chat-model").value = "openai/gpt-4.1-mini";
    $("#video-edit-provider").value = "fal";
    updateSourceModelFields();
    if (!isConfigured("fal") && !$("#key-fal").value.trim()) $("#key-fal").focus();
  });

  async function initialize() {
    setMode("ask");
    const results = await Promise.allSettled([api("/api/config"), api("/api/videos"), api("/api/routing")]);
    if (results[2].status === "fulfilled") state.routing = results[2].value;
    if (results[0].status === "fulfilled") { state.config = results[0].value; renderRoster(); }
    else toast(results[0].reason.message, true);
    if (results[1].status === "fulfilled") {
      state.videos = Array.isArray(results[1].value.videos) ? results[1].value.videos : [];
      for (const video of state.videos) state.conversations.set(video.id, storedMessages(video.id));
      renderAssetSelect();
      let lastVideo;
      try { lastVideo = localStorage.getItem(activeKey); } catch {}
      if (state.videos.length) await selectVideo(state.videos.find((video) => video.id === lastVideo) || state.videos[0]);
    } else toast(results[1].reason.message, true);
    if (state.config && !state.config.ffmpeg) toast("FFmpeg is unavailable. Install FFmpeg and restart the server to process videos.", true);
    updateControls();
    renderRouting();
    await refreshEnvironments();
    setInterval(refreshEnvironments, 3000);
  }
  initialize();
})();
