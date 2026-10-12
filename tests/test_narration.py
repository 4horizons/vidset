import asyncio
import copy
import hashlib
import io
import json
import math
import struct
import time
import wave

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, media, narration, providers


def wav_bytes(seconds):
    result = io.BytesIO()
    with wave.open(result, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"".join(struct.pack("<h", int(2500 * math.sin(i * 2 * math.pi * 220 / 24000)))
                                  for i in range(int(seconds * 24000))))
    return result.getvalue()


def probe_duration(path, stream):
    value = json.loads(media.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]).stdout)
    return float(next(s for s in value["streams"] if s["codec_type"] == stream)["duration"])


def wait_job(client, response):
    assert response.status_code == 200, response.text
    for _ in range(1000):
        job = client.get('/api/jobs/' + response.json()['job_id']).json()
        if job['status'] in ('completed', 'failed'):
            return job
        time.sleep(.01)
    pytest.fail('Narration job timed out')


@pytest.fixture
def clip(tmp_path):
    if not media.available():
        pytest.skip('Requires FFmpeg')
    source = tmp_path / 'source.mp4'
    media.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=green:s=96x160:r=24:d=1.5',
               '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1.5',
               '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source)])
    return source


@pytest.mark.parametrize('seconds,keep_original', [(.5, True), (2.5, True), (2.5, False)])
def test_export_keeps_all_words_and_preserves_source(clip, seconds, keep_original):
    original_hash = hashlib.sha256(clip.read_bytes()).hexdigest()
    speech = clip.parent / 'voice.wav'
    speech.write_bytes(wav_bytes(seconds))
    output = clip.parent / 'narrated.mp4'
    result = narration.assemble(clip, speech, output, keep_original)
    expected = max(1.5, seconds)
    assert result['duration'] == pytest.approx(expected, abs=.05)
    assert result['hold_seconds'] == pytest.approx(max(0, seconds - 1.5), abs=.05)
    assert probe_duration(output, 'video') == pytest.approx(expected, abs=.05)
    assert probe_duration(output, 'audio') == pytest.approx(expected, abs=.05)
    assert hashlib.sha256(clip.read_bytes()).hexdigest() == original_hash


def test_silent_edit_receives_voice(clip):
    silent = clip.parent / 'silent.mp4'
    media.run(['ffmpeg', '-v', 'error', '-y', '-i', str(clip), '-an', '-c:v', 'copy', str(silent)])
    voice = clip.parent / 'voice.wav'
    voice.write_bytes(wav_bytes(.8))
    output = clip.parent / 'out.mp4'
    narration.assemble(silent, voice, output, True)
    assert probe_duration(output, 'audio') == pytest.approx(1.5, abs=.05)


def test_speech_request_is_text_only_and_errors_hide_provider_body(tmp_path, monkeypatch):
    real_client = httpx.AsyncClient
    requests = []
    events = []
    async def emit(*args, **kwargs): events.append(args)
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=wav_bytes(.5))
    monkeypatch.setattr(narration.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    config = {'keys': {'openai': 'test-secret'}}
    path = tmp_path / 'voice.wav'
    asyncio.run(narration.generate_speech(config, 'A green can.', 'marin', 'natural', path, emit))
    assert requests[0] == {'model': narration.MODEL, 'input': 'A green can.', 'voice': 'marin',
                           'instructions': narration.STYLES['natural'], 'response_format': 'wav'}
    assert narration.audio_duration(path) == .5
    assert events[-1][4] == 'completed'
    monkeypatch.setattr(narration.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text='test-secret')), **kwargs))
    with pytest.raises(providers.ProviderError) as error:
        asyncio.run(narration.generate_speech(config, 'A green can.', 'marin', 'natural', path, emit))
    assert 'test-secret' not in str(error.value)
    assert not path.with_suffix('.part').exists()


def test_narration_api_reuses_speech_after_mix_failure_and_survives_restart(clip, tmp_path, monkeypatch):
    video_id, edit_id = 'a' * 32, 'b' * 32
    data = tmp_path / 'data'
    folder = data / video_id / 'edits' / edit_id
    folder.mkdir(parents=True)
    (folder / 'output.mp4').write_bytes(clip.read_bytes())
    (folder / 'edit.json').write_text(json.dumps({'id':edit_id,'video_id':video_id,'status':'completed'}))
    monkeypatch.setattr(main, 'DATA', data)
    config = copy.deepcopy(main.CONFIG)
    config['keys'] = {k: '' for k in config['keys']}
    config['keys']['openai'] = 'fake-key'
    monkeypatch.setattr(main, 'CONFIG', config)
    main.JOBS.clear()
    main.VIDEO_LOCKS.clear()
    calls = []
    async def speech(config, script, voice, style, output, emit):
        calls.append(script)
        output.write_bytes(wav_bytes(2.5))
        await emit('narration_speech', 'openai', narration.MODEL, 'Test voice generated', 'completed')
        return 2.5
    monkeypatch.setattr(narration, 'generate_speech', speech)
    real_assemble = narration.assemble
    def fail_once(*args):
        monkeypatch.setattr(narration, 'assemble', real_assemble)
        raise media.MediaError('Local encoder interrupted')
    monkeypatch.setattr(narration, 'assemble', fail_once)
    url = f'/api/videos/{video_id}/edits/{edit_id}/narrations'
    body = {'script':'The green can is revealed.', 'voice':'marin', 'keep_original':True}
    with TestClient(main.app) as client:
        assert client.post(url, json={'script':'   '}).status_code == 400
        assert client.post(url, json={'script':'Hello', 'voice':'custom'}).status_code == 422
        first = client.post(url, json=body)
        failed = wait_job(client, first)
        assert failed['status'] == 'failed'
        second = client.post(url, json=body)
        duplicate = client.post(url, json=body)
        assert duplicate.json()['job_id'] == second.json()['job_id']
        done = wait_job(client, second)
        assert done['status'] == 'completed', done
        assert len(calls) == 1
        item = done['result']['narration']
        assert item['hold_seconds'] == pytest.approx(1, abs=.05)
        assert item['run_events'] and item['workflow']['steps']
        audio = client.get(item['audio_url'], headers={'Range':'bytes=0-19'})
        assert audio.status_code == 206 and len(audio.content) == 20
        assert client.get(item['output_url']).status_code == 200
        main.JOBS.clear()
        assert client.get(url).json()['narrations'][0]['id'] == item['id']
        cached = wait_job(client, client.post(url, json=body))
        assert cached['result']['reused'] and len(calls) == 1
        assert client.get(f'/api/videos/{"c"*32}/edits/{edit_id}/narrations').status_code == 404
        assert client.post(url, json=body, headers={'Origin':'https://elsewhere.example'}).status_code == 403


def test_fit_script_preserves_grounding_and_uses_configured_reasoning(monkeypatch):
    calls = []
    async def stage(config, provider, model, prompt, emit, *args):
        calls.append((provider, model, prompt))
        return {'script':'A green can comes into view.'}
    monkeypatch.setattr(providers, '_stage_call', stage)
    result = asyncio.run(narration.fit_script({'keys':{'openai':'test'}, 'models':{'chat_provider':'openai','chat_model':'gpt-4.1-mini'}},
                                           'A green can appears at 1.7 seconds.', 5, None))
    assert result['script'] == 'A green can comes into view.'
    assert calls[0][:2] == ('openai','gpt-4.1-mini')
    assert 'at most 10 words' in calls[0][2]
    assert 'do not add facts' in calls[0][2]
