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
from app import auth, deps, i18n, integrations, runner, web
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
    paths=['/','/jobs','/jobs/testjob','/costs','/integrations',f'/series/{sid}',f'/series/{sid}/episodes/{eid}']
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
