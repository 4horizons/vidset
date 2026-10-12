"""Local model selection governed by versioned, Prelint-reviewable product rules.

Prelint is a policy reviewer, not an inference endpoint. Never label a local
choice as a live Prelint call. Review evidence is synced from GitHub separately.
"""
from __future__ import annotations
import copy
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'model-routing.json'
RECEIPT = ROOT / '.prelint' / 'review.json'
REVIEW_FILES = ['model-routing.json', 'app/routing.py', 'prelint.json', 'docs/MODEL_ROUTING.md']
REPOSITORY = '4horizons/vidset'
REVIEW_URL = 'https://github.com/4horizons/vidset/pull/1'
CATALOG = {
 'openai-vision': {'provider':'openai','model':'gpt-4.1-mini','label':'GPT-4.1 mini','roles':['vision','reasoning']},
 'gemini': {'provider':'gemini','model':'gemini-2.5-flash','label':'Gemini Flash','roles':['vision','reasoning']},
 'fal-gemini': {'provider':'fal','model':'google/gemini-2.5-flash','label':'Gemini via fal','roles':['vision']},
 'claude': {'provider':'anthropic','model':'claude-sonnet-4-6','label':'Claude Sonnet','roles':['vision','reasoning']},
 'fal-openai': {'provider':'fal','model':'openai/gpt-4.1-mini','label':'GPT-4.1 via fal','roles':['reasoning']},
 'gpt-image': {'provider':'openai','model':'gpt-image-1.5','label':'GPT Image 1.5','roles':['image']},
 'higgsfield-seedance': {'provider':'higgsfield','model':'bytedance/seedance-2.5/video-edit','label':'Seedance · Higgsfield','roles':['source_video']},
 'fal-seedance': {'provider':'fal','model':'bytedance/seedance-2.5/reference-to-video','label':'Seedance · fal','roles':['source_video']},
 'fal-kling': {'provider':'fal','model':'fal-ai/kling-video/o1/image-to-video','label':'Kling O1','roles':['anchors']},
}
TITLES = {'vision':'Evidence','reasoning':'Reasoning','image':'Still preview','source_video':'Video edit','anchors':'Motion'}
REASONS = {'vision':'Timestamped visual evidence','reasoning':'Answers and edit planning','image':'Edits the source still','source_video':'Accepts source video and camera motion','anchors':'Uses first and last frames'}


def load_policy():
    value = json.loads(POLICY.read_text())
    if value.get('version') != 1 or type(value.get('prefer_configured')) is not bool:
        raise ValueError('Invalid model-routing policy.')
    for role in TITLES:
        candidates = value.get('roles', {}).get(role)
        if not isinstance(candidates, list) or not candidates or len(candidates) != len(set(candidates)):
            raise ValueError('Invalid model-routing candidate list.')
        if any(item not in CATALOG or role not in CATALOG[item]['roles'] for item in candidates):
            raise ValueError('Model-routing policy contains an incompatible model.')
    return value


def policy_hash():
    digest = hashlib.sha256()
    for name in REVIEW_FILES:
        digest.update(name.encode() + b'\0' + (ROOT / name).read_bytes() + b'\0')
    return digest.hexdigest()


def review_status():
    result = {'state':'pending','label':'Prelint · pending','url':REVIEW_URL,'source':'local_policy'}
    try:
        receipt = json.loads(RECEIPT.read_text())
        head = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True,stderr=subprocess.DEVNULL,timeout=2).strip()
        dirty = subprocess.run(['git','diff','--quiet','HEAD','--',*REVIEW_FILES],cwd=ROOT,capture_output=True,timeout=2).returncode
        if receipt.get('repository') == REPOSITORY and receipt.get('head_sha') == head and receipt.get('policy_sha256') == policy_hash() and not dirty:
            # The sync command accepts only checks owned by the Prelint GitHub App.
            if receipt.get('app_slug') == 'prelint' and receipt.get('conclusion') == 'success' and receipt.get('status') == 'completed':
                result.update(state='reviewed',label='Prelint · reviewed',source='prelint_reviewed_policy',check_id=receipt.get('check_id'))
            elif receipt.get('app_slug') == 'prelint':
                result.update(state='attention',label='Prelint · review needed')
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def current_route(config, role):
    models = config['models']
    if role in ('vision','reasoning'):
        prefix = 'chat' if role == 'reasoning' else 'vision'
        return models.get(prefix+'_provider'), models.get(prefix+'_model')
    if role == 'image': return 'openai', models.get('image_model')
    if role == 'anchors': return 'fal', models.get('video_model')
    provider = models.get('video_edit_provider','higgsfield')
    return provider, models.get('fal_video_edit_model' if provider == 'fal' else 'video_edit_model')


def set_route(config, role, provider, model):
    models = config['models']
    if role in ('vision','reasoning'):
        prefix = 'chat' if role == 'reasoning' else 'vision'
        models[prefix+'_provider'], models[prefix+'_model'] = provider, model
    elif role == 'image': models['image_model'] = model
    elif role == 'anchors': models['video_model'] = model
    else:
        models['video_edit_provider'] = provider
        models['fal_video_edit_model' if provider == 'fal' else 'video_edit_model'] = model


def choose(config, role, policy, edit=None):
    preferred = current_route(config, role)
    pinned = role == 'source_video' and edit and edit.get('generation_provider') and edit.get('generation_model')
    if pinned: preferred = (edit['generation_provider'], edit['generation_model'])
    order = policy['roles'][role]
    available = [item for item in order if config['keys'].get(CATALOG[item]['provider'])]
    configured = next((item for item in available if (CATALOG[item]['provider'], CATALOG[item]['model']) == preferred), None)
    selected = configured if (policy['prefer_configured'] or pinned) and configured else next(iter(available), None)
    if pinned and not configured:
        # Do not replace a submitted request with a differently billed provider.
        selected = None
    chosen = {'id':selected,**CATALOG[selected]} if selected else None
    if pinned and not chosen:
        chosen = {'id':'saved-request','provider':preferred[0],'model':preferred[1],'label':'Saved request','roles':[role]}
    if chosen: set_route(config, role, chosen['provider'], chosen['model'])
    candidates = []
    for identifier, item in CATALOG.items():
        compatible = identifier in order
        keyed = bool(config['keys'].get(item['provider']))
        status = 'selected' if identifier == selected else 'incompatible' if not compatible else 'no_key' if not keyed else 'available'
        reason = 'Selected' if status == 'selected' else 'Different input type' if status == 'incompatible' else 'Key needed' if status == 'no_key' else 'Alternative route'
        candidates.append({'id':identifier,'label':item['label'],'provider':item['provider'],'model':item['model'],'status':status,'reason':reason})
    reason = 'Resumes the saved request' if pinned else REASONS[role] + (' · preferred' if configured == selected and selected else ' · available route')
    return {'role':role,'title':TITLES[role],'chosen':chosen,'reason':reason if chosen else 'Connect a compatible model key','candidates':candidates}


def apply(config, roles, edit=None):
    config = copy.deepcopy(config)
    policy = load_policy()
    decisions = [choose(config, role, policy, edit) for role in dict.fromkeys(roles)]
    config['_routing'] = {'review':review_status(),'decisions':decisions}
    return config


def for_job(config, kind, video=None, edit=None):
    video, edit = video or {}, edit or {}
    if video.get('demo') or edit.get('demo') or edit.get('status') == 'completed': return copy.deepcopy(config)
    roles = {'analyze':['vision'],'chat':['vision','reasoning'],'plan':(['vision'] if not video.get('analysis') else [])+['reasoning'],
             'environment_previews':['image'],'generate':['source_video'] if edit.get('strategy') == 'source_video' else ['image','anchors']}.get(kind,[])
    return apply(config, roles, edit) if roles else copy.deepcopy(config)
