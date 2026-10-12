import asyncio
import copy
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import main, providers, questions


SUGGESTIONS = ["When does the green can first appear?", "What is beside the can?", "How does the camera move?"]


def wait_job(client, response):
    assert response.status_code == 200, response.text
    for _ in range(300):
        job = client.get('/api/jobs/' + response.json()['job_id']).json()
        if job['status'] in ('completed', 'failed'):
            return job
        time.sleep(.01)
    pytest.fail('Suggestions timed out')


def test_questions_are_grounded_in_analysis_not_edit_instructions(monkeypatch):
    calls = []
    async def stage(config, provider, model, prompt, emit, *args):
        calls.append((provider, model, prompt))
        return {'questions': SUGGESTIONS}
    monkeypatch.setattr(providers, '_stage_call', stage)
    video = {'duration':5, 'analysis':{'summary':'A green can on a sill.', 'limitations':'City is unknown.'}}
    config = {'keys':{'openai':'test'}, 'models':{'chat_provider':'openai', 'chat_model':'gpt-4.1-mini'}}
    result = asyncio.run(questions.suggest(config, video, None))
    assert result['questions'] == SUGGESTIONS
    assert calls[0][:2] == ('openai', 'gpt-4.1-mini')
    assert 'never requests to edit' in calls[0][2]
    assert 'Do not assume an unverified city' in calls[0][2]
    assert 'City is unknown.' in calls[0][2]


@pytest.mark.parametrize('values', [[], ['Same?'] * 3, ['A?', 3, 'B?'], ['a' * 141, 'B?', 'C?']])
def test_invalid_suggestions_are_not_published(values, monkeypatch):
    async def stage(*args, **kwargs): return {'questions': values}
    monkeypatch.setattr(providers, '_stage_call', stage)
    with pytest.raises(providers.ProviderError):
        asyncio.run(questions.suggest({'keys':{'openai':'test'}, 'models':{'chat_provider':'openai'}},
                                      {'duration':5, 'analysis':{'summary':'A can.'}}, None))


def test_cache_reconnect_restart_failure_and_analysis_invalidation(tmp_path, monkeypatch):
    video_id = 'c' * 32
    folder = tmp_path / video_id
    folder.mkdir()
    video = {'id':video_id, 'duration':5, 'demo':False, 'analysis':{'summary':'A can.'}}
    path = folder / 'video.json'
    path.write_text(json.dumps(video))
    monkeypatch.setattr(main, 'DATA', tmp_path)
    monkeypatch.setattr(main, 'JOBS', {})
    monkeypatch.setattr(main, 'VIDEO_LOCKS', {})
    config = copy.deepcopy(main.CONFIG)
    config['keys'] = {key: ('test' if key == 'openai' else '') for key in config['keys']}
    monkeypatch.setattr(main, 'CONFIG', config)
    calls = []
    async def suggest(config, video, emit):
        calls.append(video['analysis'])
        await asyncio.sleep(.1)
        await emit('suggest_questions', 'openai', 'gpt-4.1-mini', 'Questions prepared', 'completed')
        return {'questions':SUGGESTIONS, 'model':'gpt-4.1-mini', 'provider':'openai'}
    monkeypatch.setattr(questions, 'suggest', suggest)
    url = f'/api/videos/{video_id}/questions'
    with TestClient(main.app) as client:
        assert client.get(url).json()['status'] == 'missing'
        assert len(calls) == 0  # GET never generates or charges.
        first = client.post(url)
        duplicate = client.post(url)
        assert duplicate.json() == first.json()
        done = wait_job(client, first)
        assert done['status'] == 'completed', done
        assert done['result']['questions'] == SUGGESTIONS
        assert done['result']['run_events']
        main.JOBS.clear()
        assert client.get(url).json()['questions'] == SUGGESTIONS
        assert wait_job(client, client.post(url))['status'] == 'completed'
        assert len(calls) == 1
        video['analysis']['summary'] = 'Now a blue can.'
        path.write_text(json.dumps(video))
        assert client.get(url).json()['status'] == 'missing'
        assert wait_job(client, client.post(url))['status'] == 'completed'
        assert len(calls) == 2
        # An interrupted job is a visible retry, not an automatic model loop.
        saved = json.loads((folder / 'questions.json').read_text())
        saved.update(status='generating', job_id='missing-job')
        (folder / 'questions.json').write_text(json.dumps(saved))
        assert client.get(url).json()['status'] == 'failed'
        assert len(calls) == 2
        video['analysis'] = None
        path.write_text(json.dumps(video))
        assert client.post(url).status_code == 400


def test_demo_questions_do_not_call_a_provider(monkeypatch):
    async def forbidden(*args, **kwargs): raise AssertionError('Demo called a model')
    async def emit(*args, **kwargs): pass
    monkeypatch.setattr(providers, '_stage_call', forbidden)
    result = asyncio.run(questions.suggest({}, {'demo':True}, emit))
    assert len(result['questions']) == 3
    assert result['provider'] == 'local'
