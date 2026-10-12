const test = require('node:test');
const assert = require('node:assert/strict');
const W = require('../app/static/workflow.js');
const t = (seconds) => new Date(Date.UTC(2026, 9, 11, 12, 0, seconds)).toISOString();
const workflow = { title: 'Edit the scene', steps: [
  { id: 'source', title: 'Prepare source', stages: ['source', 'upload'], complete_on: ['upload'], skip_on: ['reuse-source-upload'] },
  { id: 'generate', title: 'Generate replacement', stages: ['generate', 'queue'], complete_on: ['generate'], provider: 'higgsfield', model: 'seedance' },
  { id: 'assemble', title: 'Assemble output', stages: ['assemble'], complete_on: ['assemble'] },
] };
test('info queue updates retain active phase and actual elapsed time', () => {
  const view = W.derive({ status: 'running', workflow, events: [
    { stage: 'source', status: 'running', time: t(0) }, { stage: 'upload', status: 'completed', time: t(2) },
    { stage: 'generate', status: 'running', time: t(3), provider: 'higgsfield', model: 'actual-model' },
    { stage: 'queue', status: 'info', time: t(9), message: 'Queue position changed' },
  ] }, { now: Date.parse(t(12)) });
  assert.deepEqual(view.steps.map(s => s.status), ['completed', 'running', 'pending']);
  assert.equal(view.steps[1].elapsedMs, 9000);
  assert.equal(view.steps[1].model, 'actual-model');
  assert.equal(view.counts.completed, 1);
});
test('intermediate completed event does not complete a grouped phase', () => {
  const view = W.derive({ status: 'running', workflow, events: [{ stage: 'source', status: 'completed', time: t(0) }] });
  assert.equal(view.steps[0].status, 'running');
});
test('failed jobs stop the active phase and leave future steps unrun', () => {
  const view = W.derive({ status: 'failed', error: 'Provider rejected request', completed_at: t(10), workflow, events: [
    { stage: 'upload', status: 'completed', time: t(2) }, { stage: 'generate', status: 'running', time: t(3) },
    { stage: 'error', status: 'failed', time: t(10) },
  ] });
  assert.deepEqual(view.steps.map(s => s.status), ['completed', 'failed', 'pending']);
  assert.equal(view.steps[1].elapsedMs, 7000);
  assert.equal(view.steps[2].observed, false);
});
test('completed jobs skip planned provider calls without completion evidence', () => {
  const view = W.derive({ status: 'completed', workflow, events: [{ stage: 'assemble', status: 'completed', time: t(8) }] });
  assert.deepEqual(view.steps.map(s => s.status), ['skipped', 'skipped', 'completed']);
  assert.equal(view.counts.completed, 1);
  assert.equal(view.counts.skipped, 2);
});
test('explicit reuse is skipped rather than presented as another provider call', () => {
  const view = W.derive({ status: 'running', workflow, events: [{ stage: 'reuse-source-upload', status: 'completed', time: t(0), message: 'Using saved upload' }] });
  assert.equal(view.steps[0].status, 'skipped');
  assert.equal(view.steps[0].observed, false);
  assert.equal(view.steps[0].elapsedMs, null);
  assert.equal(view.elapsedMs, null);
});
test('informational reuse announcement alone is not terminal', () => {
  const view = W.derive({ status: 'running', workflow, events: [{ stage: 'reuse-source-upload', status: 'info', time: t(0) }] });
  assert.notEqual(view.steps[0].status, 'skipped');
});
test('saved timings remain frozen regardless of current wall time', () => {
  const job = { status: 'completed', started_at: t(1), completed_at: t(10), workflow, events: [
    { stage: 'generate', status: 'running', time: t(3) }, { stage: 'generate', status: 'completed', time: t(8) },
  ] };
  const before = W.derive(job, { saved: true, now: Date.parse(t(20)) });
  const after = W.derive(job, { saved: true, now: Date.parse(t(50)) });
  assert.equal(before.elapsedMs, 9000);
  assert.equal(after.elapsedMs, before.elapsedMs);
  assert.equal(after.steps[1].elapsedMs, 5000);
});
test('completion-only explicit provider duration is shown without fabricated starts', () => {
  const view = W.derive({ status: 'completed', workflow, events: [{ stage: 'generate', status: 'completed', time: t(20), elapsed_ms: 17000 }] });
  assert.equal(view.steps[1].startedAt, null);
  assert.equal(view.steps[1].elapsedMs, 17000);
});
test('legacy runs derive only observed stages and tolerate unknown labels', () => {
  const view = W.derive({ status: 'completed', events: [
    { stage: 'provider-special-stage', status: 'completed', message: 'Actual imported evidence' },
    { stage: 'assemble', status: 'completed' },
  ] }, { saved: true });
  assert.equal(view.derived, true);
  assert.equal(view.steps.length, 2);
  assert.equal(view.steps[0].title, 'Provider special stage');
  assert.equal(view.steps[0].elapsedMs, null);
});
test('request failure before any recorded step does not invent an LLM call', () => {
  const view = W.derive({ status: 'failed', error: 'Network unavailable', events: [] });
  assert.equal(view.steps.length, 0);
  assert.equal(view.counts.failed, 0);
  assert.equal(view.error, 'Network unavailable');
});
