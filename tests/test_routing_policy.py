import copy,json
from types import SimpleNamespace
import pytest
from app import routing,main


def config(**keys):
    c=copy.deepcopy(main.CONFIG)
    c['keys']={name:keys.get(name,'') for name in c['keys']}
    c['models'].update(vision_provider='openai',vision_model='gpt-4.1-mini',chat_provider='openai',chat_model='gpt-4.1-mini',video_edit_provider='higgsfield',video_edit_model='bytedance/seedance-2.5/video-edit')
    return c


def test_selection_executes_available_compatible_fallback_without_leaking_keys():
    source=config(fal='private-test-fal-key')
    result=routing.for_job(source,'analyze',{'demo':False})
    assert result['models']['vision_provider']=='fal'
    assert result['models']['vision_model']=='google/gemini-2.5-flash'
    decision=result['_routing']['decisions'][0]
    assert decision['chosen']['id']=='fal-gemini'
    assert next(c for c in decision['candidates'] if c['id']=='fal-kling')['status']=='incompatible'
    assert next(c for c in decision['candidates'] if c['id']=='openai-vision')['status']=='no_key'
    assert 'private-test' not in json.dumps(result['_routing'])
    assert source['models']['vision_provider']=='openai'


def test_still_preview_cannot_select_a_video_model():
    result=routing.for_job(config(fal='fake',higgsfield='fake'),'environment_previews',{'demo':False})
    assert result['_routing']['decisions'][0]['chosen'] is None
    assert result['models']['video_edit_provider']=='higgsfield'


def test_retry_cannot_reroute_a_paid_request():
    edit={'strategy':'source_video','generation_provider':'higgsfield','generation_model':'bytedance/seedance-2.5/video-edit'}
    result=routing.for_job(config(fal='fake'),'generate',{'demo':False},edit)
    assert result['models']['video_edit_provider']=='higgsfield'
    assert result['_routing']['decisions'][0]['reason']=='Resumes the saved request'


def test_demo_and_completed_edit_make_no_routing_claim():
    assert '_routing' not in routing.for_job(config(),'analyze',{'demo':True})
    assert '_routing' not in routing.for_job(config(),'generate',{}, {'status':'completed'})


@pytest.mark.parametrize('field,value',[('head_sha','other'),('policy_sha256','changed'),('app_slug','not-prelint'),('status','in_progress'),('conclusion','failure')])
def test_only_verified_matching_prelint_check_is_reviewed(tmp_path,monkeypatch,field,value):
    receipt=tmp_path/'receipt.json'
    monkeypatch.setattr(routing,'RECEIPT',receipt)
    monkeypatch.setattr(routing,'policy_hash',lambda:'hash')
    monkeypatch.setattr(routing.subprocess,'check_output',lambda *a,**k:'head\n')
    monkeypatch.setattr(routing.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    correct={'repository':'4horizons/vidset','head_sha':'head','policy_sha256':'hash','app_slug':'prelint','status':'completed','conclusion':'success','check_id':123}
    receipt.write_text(json.dumps(correct))
    assert routing.review_status()['state']=='reviewed'
    correct[field]=value;receipt.write_text(json.dumps(correct))
    assert routing.review_status()['state']!='reviewed'


def test_policy_rejects_incompatible_models(tmp_path,monkeypatch):
    value=routing.load_policy();value['roles']['image']=['fal-kling']
    p=tmp_path/'routing.json';p.write_text(json.dumps(value));monkeypatch.setattr(routing,'POLICY',p)
    with pytest.raises(ValueError,match='incompatible'):routing.load_policy()
