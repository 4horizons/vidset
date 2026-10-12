"use strict";

window.VidsetNarration = {
  create({api, runJob, safeMedia, toast, getState, sourceVideo, outputVideo}) {
    const $ = id => document.getElementById(id);
    const script = $("narration-script"), voice = $("narration-voice"), style = $("narration-style");
    const keep = $("narration-keep-audio"), versions = $("narration-version"), status = $("narration-status");
    let video = null, edit = null, saved = [], sequence = 0, loading = false;
    const key = () => `vidset.narration.draft.v1.${video?.id}.${edit?.id}`;
    const endpoint = () => `/api/videos/${video.id}/edits/${edit.id}`;
    const selected = () => saved.find(item => item.id === versions.value);
    const draft = () => ({script: script.value, voice: voice.value, style: style.value, keep_original: keep.checked});
    function saveDraft() {
      if (!edit) return;
      try { localStorage.setItem(key(), JSON.stringify(draft())); } catch { /* Draft remains in the editor. */ }
    }
    function updateControls() {
      const words = script.value.trim().split(/\s+/).filter(Boolean).length;
      $("narration-length").textContent = `${words} words · ~${Math.ceil(words / 2.3)}s`;
      $("narration-timing").textContent = video ? `Clip: ${video.duration.toFixed(1)}s` : "";
      const blocked = getState().busy || loading || !edit;
      const empty = !script.value.trim();
      $("narration-fit").disabled = blocked || empty;
      $("narration-generate").disabled = blocked || empty;
      $("narration-generate").textContent = getState().busy ? "Working…" : "Generate narration";
      for (const input of [script, voice, style, keep, versions]) input.disabled = blocked;
      $("narration-play").disabled = blocked || !selected();
    }
    function renderVersions() {
      const previous = versions.value;
      versions.replaceChildren(new Option("Original audio", ""));
      for (const item of saved.filter(item => item.status === "completed")) {
        versions.append(new Option(`${item.voice} · ${item.speech_duration.toFixed(1)}s · ${item.script.slice(0,40)}`, item.id));
      }
      versions.value = saved.some(item => item.id === previous && item.status === "completed") ? previous : "";
      $("narration-saved").hidden = !saved.some(item => item.status === "completed");
      updateLinks();
      updateControls();
    }
    function updateLinks() {
      const item = selected();
      for (const id of ["narration-download", "narration-audio-download"]) $(id).hidden = !item;
      if (!item) return;
      $("narration-download").href = safeMedia(item.output_url);
      $("narration-download").download = `vidset-narrated-${item.id}.mp4`;
      $("narration-audio-download").href = safeMedia(item.audio_url);
      $("narration-audio-download").download = `vidset-voice-${item.id}.wav`;
    }
    function switchPlayback() {
      const item = selected();
      const url = safeMedia(item?.output_url || edit?.output_url);
      outputVideo.pause();
      sourceVideo.pause();
      if (url && outputVideo.src !== url) { outputVideo.src = url; outputVideo.load(); }
      $("download-output").href = url;
      $("download-output").download = item ? `vidset-narrated-${item.id}.mp4` : `vidset-${edit.id}.mp4`;
      status.textContent = item ? `AI voice · ${item.voice}. ${item.hold_seconds > .05 ? `Last frame held for ${item.hold_seconds.toFixed(1)}s.` : "Voice and video are synchronized."}` : "Playing the edited video with its original audio.";
      updateLinks(); updateControls();
    }
    async function selectEdit(nextVideo, nextEdit) {
      if (edit?.id === nextEdit.id && video?.id === nextVideo.id) {
        versions.value = ""; updateLinks(); updateControls(); return;
      }
      saveDraft();
      const ticket = ++sequence;
      video = nextVideo; edit = nextEdit; saved = []; loading = true;
      script.value = ""; voice.value = "marin"; style.value = "natural"; keep.checked = true;
      status.textContent = "";
      try {
        const stored = JSON.parse(localStorage.getItem(key()) || "{}");
        if (typeof stored.script === "string") script.value = stored.script.slice(0,2000);
        if ([...voice.options].some(option => option.value === stored.voice)) voice.value = stored.voice;
        if ([...style.options].some(option => option.value === stored.style)) style.value = stored.style;
        if (typeof stored.keep_original === "boolean") keep.checked = stored.keep_original;
      } catch { /* An invalid local draft should not block playback. */ }
      renderVersions();
      try {
        const data = await api(`${endpoint()}/narrations`);
        if (sequence !== ticket) return;
        saved = data.narrations || [];
        const active = saved.find(item => item.status === "generating");
        if (active) status.textContent = "A narration is still generating. Use the same settings to reconnect; no duplicate call is made.";
      } catch (error) { if (sequence === ticket) status.textContent = `Could not load saved narrations: ${error.message}`; }
      finally { if (sequence === ticket) { loading = false; renderVersions(); } }
    }
    function useReply(text) {
      const next = [script.value.trim(), text.trim()].filter(Boolean).join("\n\n");
      if (next.length > 2000) { toast("The script can hold 2,000 characters. Shorten it before adding another reply.", true); return; }
      script.value = next;
      $("narration-panel").open = true;
      $("narration-panel").scrollIntoView({behavior: "smooth", block: "center"});
      script.focus({preventScroll: true});
      status.textContent = "Reply added. Edit the script, or fit it to the clip before generating.";
      saveDraft(); updateControls();
    }
    $("narration-fit").addEventListener("click", async () => {
      if (getState().busy || loading || !edit || !script.value.trim()) return;
      const ticket = sequence, path = endpoint();
      saveDraft();
      try {
        const result = await runJob(`${path}/narration-script`, {text: script.value}, "Fit narration script");
        if (sequence !== ticket) return;
        script.value = result.script;
        status.textContent = "Script shortened. Review it before generating the voice.";
        saveDraft();
      } catch (error) { if (sequence === ticket) { status.textContent = error.message; toast(error.message, true); } }
      finally { updateControls(); }
    });
    $("narration-generate").addEventListener("click", async () => {
      if (getState().busy || loading || !edit || !script.value.trim()) return;
      const ticket = sequence, path = endpoint(), body = draft();
      saveDraft(); outputVideo.pause(); sourceVideo.pause();
      try {
        const result = await runJob(`${path}/narrations`, body, "Generate voice narration");
        if (sequence !== ticket) return;
        saved = [result.narration, ...saved.filter(item => item.id !== result.narration.id)];
        renderVersions(); versions.value = result.narration.id; switchPlayback();
        $("narration-play").focus({preventScroll: true});
        toast("Narrated video ready. Press Play narrated video to listen.");
      } catch (error) { if (sequence === ticket) { status.textContent = error.message; toast(error.message, true); } }
      finally { updateControls(); }
    });
    $("narration-play").addEventListener("click", async () => {
      if (!selected()) return;
      sourceVideo.pause(); outputVideo.muted = false; outputVideo.volume = 1;
      try { outputVideo.currentTime = 0; await outputVideo.play(); }
      catch { toast("Use the video’s play button to hear the narration."); }
      outputVideo.scrollIntoView({behavior: "smooth", block: "center"});
    });
    versions.addEventListener("change", switchPlayback);
    for (const input of [script, voice, style, keep]) input.addEventListener("input", () => { saveDraft(); updateControls(); });
    return {selectEdit, useReply, updateControls, reset() {
      saveDraft(); sequence++; video = null; edit = null; saved = []; loading = false;
      script.value = ""; status.textContent = ""; $("narration-panel").open = false;
      renderVersions();
    }};
  },
};
