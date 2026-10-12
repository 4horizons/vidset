(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VideoWorkflow = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const TERMINAL_JOB = new Set(["completed", "failed", "cancelled"]);
  const stageTitles = {
    upload: "Import your video", normalize: "Prepare the source video", frames: "Extract evidence frames",
    proxy: "Prepare the analysis clip", understand: "Understand the scene", retrieve: "Find the relevant moment",
    verify: "Verify the visual evidence", answer: "Answer your question", evidence: "Attach the evidence",
    plan: "Plan the edit", anchors: "Capture boundary frames", "edit-anchors": "Edit boundary images",
    "edit-start": "Edit the first frame", "edit-end": "Edit the last frame", conditioning: "Prepare generation inputs",
    "source-clip": "Cut the source moment", "source-upload": "Upload the source clip",
    "source-edit": "Edit the source video", generate: "Generate the replacement", stitch: "Assemble the final video",
    render: "Render the new cut", review: "Review the edited scene", demo: "Open the scripted sample", error: "Run stopped",
  };
  const list = (value) => Array.isArray(value) ? value.filter((item) => typeof item === "string" && item.length) : [];
  const eventsOf = (job) => Array.isArray(job?.events) ? job.events.filter((event) => event && typeof event === "object") : [];
  const string = (value) => typeof value === "string" ? value : "";
  const timestamp = (value) => {
    if (typeof value === "number" && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
    if (typeof value !== "string" || !value) return null;
    const result = Date.parse(value);
    return Number.isFinite(result) ? result : null;
  };
  const firstTime = (events) => events.map((event) => timestamp(event.time)).find((value) => value !== null) ?? null;
  const lastTime = (events) => firstTime([...events].reverse());
  const titleForStage = (stage) => stageTitles[stage] || string(stage).replaceAll("_", " ").replaceAll("-", " ").replace(/^./, (letter) => letter.toUpperCase()) || "Recorded activity";

  function buildWorkflow(job) {
    const supplied = job?.workflow;
    if (supplied && Array.isArray(supplied.steps) && supplied.steps.length) {
      return {
        title: string(supplied.title) || "Your video workflow",
        derived: false,
        steps: supplied.steps.map((step, index) => {
          const stages = list(step.stages);
          return {
            id: string(step.id) || `step-${index + 1}`,
            title: string(step.title) || `Step ${index + 1}`,
            provider: string(step.provider), model: string(step.model),
            stages, complete_on: Array.isArray(step.complete_on) ? list(step.complete_on) : stages,
            skip_on: list(step.skip_on),
          };
        }),
      };
    }
    const stages = [...new Set(eventsOf(job).map((event) => string(event.stage)).filter(Boolean))];
    return {
      title: string(supplied?.title) || (job?.saved ? "Recorded video workflow" : "Your video workflow"),
      derived: true,
      steps: stages.map((stage) => {
        const observed = eventsOf(job).filter((event) => event.stage === stage);
        const identity = [...observed].reverse().find((event) => event.model || event.provider) || {};
        return { id: `observed-${stage}`, title: titleForStage(stage), provider: string(identity.provider), model: string(identity.model), stages: [stage], complete_on: [stage], skip_on: [] };
      }),
    };
  }

  function elapsedFor(step, job, now, saved) {
    if (step.status === "pending" || step.status === "skipped") return null;
    const explicit = [...step.events].reverse().find((event) => ["completed", "failed"].includes(event.status) && Number.isFinite(event.elapsed_ms) && event.elapsed_ms >= 0);
    if (step.startedAt !== null && step.endedAt !== null) return Math.max(0, step.endedAt - step.startedAt);
    if (["completed", "failed"].includes(step.status) && explicit) return explicit.elapsed_ms;
    if (step.startedAt === null) return null;
    if (step.status === "running") {
      const end = saved || job.stale || TERMINAL_JOB.has(job.status)
        ? timestamp(job.completed_at) ?? lastTime(eventsOf(job))
        : now;
      return end === null ? null : Math.max(0, end - step.startedAt);
    }
    return null;
  }

  function derive(job = {}, options = {}) {
    const saved = Boolean(options.saved ?? job.saved);
    const now = Number.isFinite(options.now) ? options.now : Date.now();
    const events = eventsOf(job);
    const workflow = buildWorkflow(job);
    const steps = workflow.steps.map((definition) => {
      const relevant = events.filter((event) => definition.stages.includes(event.stage) || definition.complete_on.includes(event.stage) || definition.skip_on.includes(event.stage));
      const mapped = relevant.filter((event) => !definition.skip_on.includes(event.stage));
      const completion = [...relevant].reverse().find((event) =>
        (event.status === "failed" && !definition.skip_on.includes(event.stage)) ||
        (event.status === "completed" && definition.complete_on.includes(event.stage)) ||
        (["completed", "skipped"].includes(event.status) && definition.skip_on.includes(event.stage)));
      let status = relevant.length ? "running" : "pending";
      let reason = "";
      if (completion) {
        if (definition.skip_on.includes(completion.stage)) { status = "skipped"; reason = string(completion.message) || "Existing work was reused."; }
        else if (completion.status === "failed") status = "failed";
        else status = "completed";
        // A later explicit start belongs to a retry or the next observed part of this step.
        const terminalIndex = relevant.indexOf(completion);
        if (relevant.slice(terminalIndex + 1).some((event) => event.status === "running" && definition.stages.includes(event.stage))) status = "running";
      }
      const startedAt = firstTime(mapped.filter((event) => event.status === "running")) ?? firstTime(mapped.filter((event) => event.status === "info"));
      let endedAt = ["completed", "failed", "skipped"].includes(status) ? timestamp(completion?.time) : null;
      if (job.status === "completed" && !["completed", "failed", "skipped"].includes(status)) {
        status = "skipped";
        reason = relevant.length ? "No completion was recorded for this step." : "This step was not run in this job.";
        endedAt = null;
      }
      if (job.status === "failed" && status === "running") {
        status = "failed";
        endedAt = timestamp(job.completed_at) ?? lastTime(events);
        reason = string(job.error) || "The run stopped during this step.";
      }
      const latestEvent = relevant.at(-1) || null;
      const observedIdentity = [...mapped].reverse().find((event) => event.provider || event.model);
      const artifacts = [];
      const urls = new Set();
      for (const event of relevant) {
        for (const artifact of Array.isArray(event.artifacts) ? event.artifacts : []) {
          if (artifact?.url && !urls.has(artifact.url)) { urls.add(artifact.url); artifacts.push(artifact); }
        }
      }
      const step = {
        ...definition, status, reason, events: relevant, latestEvent, artifacts,
        provider: string(observedIdentity?.provider) || definition.provider,
        model: string(observedIdentity?.model) || definition.model,
        observed: mapped.length > 0, startedAt, endedAt,
      };
      step.elapsedMs = elapsedFor(step, job, now, saved);
      return step;
    });
    const counts = { total: steps.length, completed: 0, running: 0, failed: 0, skipped: 0, pending: 0 };
    for (const step of steps) counts[step.status]++;
    const startedAt = timestamp(job.started_at) ?? firstTime(events.filter((event) => event.status === "running"));
    const endedAt = timestamp(job.completed_at) ?? ((saved || TERMINAL_JOB.has(job.status) || job.stale) ? lastTime(events) : null);
    const timingEnd = endedAt ?? (saved || job.stale || TERMINAL_JOB.has(job.status) ? null : now);
    return {
      id: string(job.id), title: workflow.title, derived: workflow.derived, status: string(job.status) || "queued", saved,
      stale: Boolean(job.stale), error: string(job.error), steps, counts,
      progressLabel: `${counts.completed} / ${counts.total} steps completed`,
      activeStepId: steps.find((step) => step.status === "running")?.id || null,
      startedAt, endedAt, elapsedMs: startedAt === null || timingEnd === null ? null : Math.max(0, timingEnd - startedAt),
    };
  }

  return { derive, buildWorkflow, titleForStage, timestamp };
});
