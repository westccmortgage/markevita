"""Request-scoped UI language and explicit production speech language, offline."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import nodes
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import auth, deps, i18n, integrations, runner, web, live_jobs
from app.config import settings
from serial import providers
from test_live_episodes import short_package

@pytest.fixture
def language_ui(short_package, monkeypatch):
    store,sid,eid=short_package
    monkeypatch.setattr(integrations,'store',store)
    monkeypatch.setattr(settings,'base_path','/studio')
    monkeypatch.setattr(settings,'public_url','https://studio.example.test')
    monkeypatch.setitem(deps.templates.env.globals,'base','/studio')
    monkeypatch.setattr(runner,'episode_runtime',lambda *a:{'status':'draft','spent_usd':0,'reserved_usd':0,'takes':{},'masters':[],'stages':{},'approvals':{},'overrides':[],'qa':[]})
    application=FastAPI();application.include_router(web.router,prefix='/studio')
    client=TestClient(application,base_url='https://studio.example.test',follow_redirects=False)
    client.cookies.set(auth.COOKIE,auth.serialize({'email':'admin@example.test','role':'owner'}))
    yield client,store,sid,eid
    client.close()


def test_language_cookie_is_scoped_persistent_and_does_not_change_production(language_ui):
    client,store,sid,eid=language_ui
    before=copy.deepcopy(store.list('series'))
    response=client.post('/studio/ui-language',data={'language':'ru','next':f'/studio/series/{sid}'})
    assert response.status_code==303 and response.headers['location']==f'/studio/series/{sid}'
    cookie=response.headers['set-cookie']
    assert 'Path=/studio' in cookie and 'Max-Age=31536000' in cookie and 'Secure' in cookie
    page=client.get('/studio/jobs')
    assert '<html lang="ru">' in page.text and 'Задачи производства' in page.text
    assert 'value="/studio/jobs"' in page.text and '/studio/studio/' not in page.text
    assert before==store.list('series')
    # A different user's browser has an independent preference.
    with TestClient(client.app,base_url='https://studio.example.test') as other:
        other.cookies.set(auth.COOKIE,auth.serialize({'email':'other@example.test','role':'owner'}))
        assert '<html lang="en">' in other.get('/studio/jobs').text
    client.post('/studio/ui-language',data={'language':'en','next':'/studio/jobs'})
    assert '<html lang="en">' in client.get('/studio/jobs').text


@pytest.mark.parametrize('target',['https://evil.example/','//evil.example','/studio/../other','/%2f/evil.example','/studio/%252e%252e/other','/studio/\\evil','/studio/%0d%0aevil','/outside'])
def test_language_switch_cannot_redirect_off_studio(language_ui,target):
    client,*_=language_ui
    response=client.post('/studio/ui-language',data={'language':'ru','next':target})
    assert response.headers['location']=='/studio/'


def test_invalid_language_does_not_set_cookie(language_ui):
    client,*_=language_ui
    r=client.post('/studio/ui-language',data={'language':'arbitrary','next':'/studio/'})
    assert r.status_code==400 and 'set-cookie' not in r.headers


def test_all_studio_pages_render_russian_without_translating_content(language_ui):
    client,store,sid,eid=language_ui
    client.post('/studio/ui-language',data={'language':'ru','next':'/studio/'})
    store.update('series',{'id':sid},{'title':'My English Film'})
    store.update('episodes',{'series_id':sid,'episode_id':eid},{'spent_usd':0,'status':'draft'})
    store.insert('production_jobs',{'id':'testjob','series_id':sid,'episode_id':eid,'state':'failed','mode':'live',
         'stages':['intake','references'],'progress':{'stage':'references','done':['intake']},'error':'Production stopped. Saved requests are retained; Resume will not resubmit an uncertain request.', 'log':'Original provider log'})
    paths=['/','/jobs','/jobs/testjob','/costs','/integrations',f'/series/{sid}',f'/series/{sid}/episodes/{eid}/studio']
    paths += [f'/series/{sid}/{p}' for p in ['characters','locations','world','references']]
    for path in paths:
        r=client.get('/studio'+path)
        assert r.status_code==200,(path,r.text[:300])
        assert '<html lang="ru">' in r.text,path
        assert 'value="ru"' in r.text,path
    series=client.get(f'/studio/series/{sid}').text
    assert 'My English Film' in series and 'Язык озвучки' in series
    assert 'value="en-US" selected' in series
    job=client.get('/studio/jobs/testjob').text
    assert 'Ошибка' in job and 'Референсы' in job and 'Original provider log' in job
    assert 'Производство остановлено' in job
    assert store.list('series')[0]['language']=='en-US'
    client.cookies.delete(auth.COOKIE)
    for path in ['login','forgot','reset']:
        r=client.get('/studio/'+path)
        assert r.status_code==200 and '<html lang="ru">' in r.text
    reset=client.get('/studio/reset?token_hash=offline-hash').text
    assert 'value="offline-hash"' in reset
    assert 'action="/studio/ui-language"' not in reset  # do not discard a recovery token


def test_static_translation_keys_exist_and_all_templates_compile():
    env=deps.templates.env
    for name in env.list_templates():
        source=env.loader.get_source(env,name)[0]
        parsed=env.parse(source)
        env.get_template(name)
        for call in parsed.find_all(nodes.Call):
            if isinstance(call.node,nodes.Name) and call.node.name=='_' and call.args and isinstance(call.args[0],nodes.Const):
                assert call.args[0].value in i18n.RU,(name,call.args[0].value)
                assert i18n.RU[call.args[0].value].strip()


@pytest.mark.parametrize('language', ['en', 'ru'])
@pytest.mark.parametrize('legacy', [False, True])
def test_reference_review_pause_links_to_existing_images_without_changing_job(language_ui, monkeypatch, language, legacy):
    client, store, sid, eid = language_ui
    client.cookies.set(i18n.COOKIE, language)
    progress = {'stage': 'references', 'done': ['intake', 'direction']}
    if not legacy:
        progress.update(waiting_for='reference_approval', done=['intake', 'direction', 'references'])
    store.insert('production_jobs', {
        'id': 'reviewjob', 'series_id': sid, 'episode_id': eid, 'state': 'paused', 'mode': 'live',
        'stages': ['intake', 'direction', 'references', 'keyframes', 'video'], 'progress': progress,
        'log': live_jobs.REFERENCE_APPROVAL_MESSAGE if legacy else '',
    })
    before = copy.deepcopy(store.get('production_jobs', {'id': 'reviewjob'}))
    start = Mock(side_effect=AssertionError('A GET must not start production'))
    approve = Mock(side_effect=AssertionError('A GET must not approve references'))
    monkeypatch.setattr(live_jobs, 'start', start)
    monkeypatch.setattr(live_jobs, 'approve_references', approve)
    for _ in range(2):
        page = client.get('/studio/jobs/reviewjob')
        assert page.status_code == 200
        assert page.headers['cache-control'] == 'no-store'
        assert 'id="reference-review-title"' in page.text
        assert f'class="btn btn-primary" href="/studio/series/{sid}/references"' in page.text
        assert f'class="btn" href="/studio/series/{sid}/episodes/{eid}"' in page.text
        assert ('Открыть референсы' if language == 'ru' else 'Open references') in page.text
        assert ('Проверка сценария, Режиссура, Референсы' if language == 'ru' else 'intake, direction, references') in page.text
        assert ('нажмите «Продолжить»' if language == 'ru' else 'click Resume') in page.text
    assert store.get('production_jobs', {'id': 'reviewjob'}) == before
    start.assert_not_called()
    approve.assert_not_called()


@pytest.mark.parametrize('state,stage,log', [
    ('paused', 'references', ''),  # A manual pause is not a completed pack.
    ('failed', 'references', live_jobs.REFERENCE_APPROVAL_MESSAGE),
    ('running', 'references', live_jobs.REFERENCE_APPROVAL_MESSAGE),
    ('paused', 'video', live_jobs.REFERENCE_APPROVAL_MESSAGE),
])
def test_unrelated_jobs_do_not_claim_references_are_ready(language_ui, state, stage, log):
    client, store, sid, eid = language_ui
    store.insert('production_jobs', {
        'id': 'otherjob', 'series_id': sid, 'episode_id': eid, 'state': state, 'mode': 'live',
        'stages': ['references', 'video'], 'progress': {'stage': stage, 'done': []}, 'log': log,
    })
    page = client.get('/studio/jobs/otherjob')
    assert page.status_code == 200 and 'id="reference-review-title"' not in page.text


def test_russian_tts_uses_explicit_language_and_original_dialogue(tmp_path,monkeypatch):
    cfg=SimpleNamespace(elevenlabs_model_id='eleven_v3',elevenlabs_api_key='offline-only',dry_run=False)
    post=Mock(return_value=SimpleNamespace(status_code=200,content=b'offline audio',headers={}))
    monkeypatch.setattr(providers.requests,'post',post)
    dialogue='Завтра он женится на мне. Почему он целует мою сестру?'
    result=providers.tts(cfg,lambda _:None,dialogue,'whispers','offline-voice',tmp_path/'voice.mp3',language_code='ru-RU')
    assert post.call_args.kwargs['json']['language_code']=='ru'
    assert dialogue in post.call_args.kwargs['json']['text']
    assert result['language_code']=='ru' and result['text']==dialogue


def test_multilingual_v2_uses_original_text_without_unsupported_parameter(tmp_path,monkeypatch):
    cfg=SimpleNamespace(elevenlabs_model_id='eleven_multilingual_v2',elevenlabs_api_key='offline-only',dry_run=False)
    post=Mock(return_value=SimpleNamespace(status_code=200,content=b'offline audio',headers={}))
    monkeypatch.setattr(providers.requests,'post',post)
    providers.tts(cfg,lambda _:None,'Привет!','','offline-voice',tmp_path/'voice.mp3',language_code='ru-RU')
    assert 'language_code' not in post.call_args.kwargs['json']
    assert post.call_args.kwargs['json']['text']=='Привет!'


def test_an_incomplete_series_renders_its_blockers_in_russian(language_ui):
    """Every earlier page test used a complete series, so the list of what is
    missing was rendered zero times and a crash in it reached production."""
    client, store, sid, eid = language_ui
    client.post('/studio/ui-language', data={'language': 'ru', 'next': '/studio/'})
    store.update('episodes', {'series_id': sid, 'episode_id': eid},
                 {'spent_usd': 0, 'status': 'draft'})
    store.upsert('characters', {'series_id': sid, 'character_id': 'nora', 'name': 'Nora',
                                'visual': True, 'appearance': ''})
    store.delete('clothing', {'series_id': sid, 'character_id': 'nora'})
    for path in (f'/series/{sid}', f'/series/{sid}/characters',
                 f'/series/{sid}/episodes/{eid}/studio'):
        r = client.get('/studio' + path)
        assert r.status_code == 200, (path, r.text[:400])
        assert 'Nora' in r.text, path
    page = client.get(f'/studio/series/{sid}').text
    assert 'описание внешности' in page and 'Заполнить автоматически' in page


@pytest.mark.parametrize("state", ["running", "failed", "done"])
def test_the_progress_banners_render_in_every_state(language_ui, monkeypatch, state):
    """A macro imported without context cannot translate, and the banner only
    renders while work is running — so every earlier test skipped its body."""
    from app import authoring, web
    client, store, sid, eid = language_ui
    client.post('/studio/ui-language', data={'language': 'ru', 'next': '/studio/'})
    store.update('episodes', {'series_id': sid, 'episode_id': eid},
                 {'spent_usd': 0, 'status': 'draft'})
    reported = {'state': state, 'error': 'the model refused', 'changed': ['sc01 (speech)'],
                'added': [], 'completed': ['nora'], 'locations': []}
    monkeypatch.setattr(authoring, 'work_state', lambda *a, **k: reported)
    monkeypatch.setattr(authoring, 'fill_state', lambda sid: reported)
    for path in (f'/series/{sid}', f'/series/{sid}/characters',
                 f'/series/{sid}/episodes/{eid}/studio'):
        r = client.get('/studio' + path)
        assert r.status_code == 200, (path, state, r.text[:400])
        assert '<html lang="ru">' in r.text
    page = client.get(f'/studio/series/{sid}/episodes/{eid}/studio').text
    if state == 'running':
        assert 'Студия пишет эту серию' in page and 'http-equiv="refresh"' in page
    elif state == 'failed':
        assert 'остановилось' in page and 'the model refused' in page
    else:
        assert 'sc01 (speech)' in page


def test_the_self_check_finds_the_screens_it_checks(language_ui, monkeypatch):
    """It reported every screen as broken: it asked for the proxy's prefix,
    which the routes do not carry in-process, and got a 404 each time."""
    from app import selfcheck
    client, store, sid, eid = language_ui
    store.update('episodes', {'series_id': sid, 'episode_id': eid},
                 {'spent_usd': 0, 'status': 'draft'})
    monkeypatch.setattr(selfcheck, 'store', store, raising=False)
    r = client.get('/studio/selfcheck')
    assert r.status_code == 200
    assert 'Not Found' not in r.text, r.text[:600]
    assert 'сломан' not in r.text
    assert f'/series/{sid}' in r.text and '/costs' in r.text


def test_opening_a_buttons_address_returns_to_the_studio():
    """Reloading after pressing a button answered {"detail":"Method Not
    Allowed"} on a blank white page, with no way back."""
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    for path in ("/series/island/fill-bible", "/series/island/episodes/s01e01/draft"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/series/island"), path
    assert client.request("PUT", "/series/island/fill-bible").status_code == 405
