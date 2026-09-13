"""No-network regression for the interrupted reference download seen in production."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import requests
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import runner  # installs the engine import path
from app.live_providers import DurableFal
from serial import providers
from serial.state import State

class Reply:
    def __init__(self, chunks, length=None):
        self.chunks=chunks
        self.headers={} if length is None else {'Content-Length': str(length)}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception): raise chunk
            yield chunk

@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(providers.time, 'sleep', lambda _: None)


def test_interrupted_get_retries_same_asset_atomically(tmp_path, monkeypatch, no_sleep):
    dest=tmp_path/'nora.png';dest.write_bytes(b'old complete image')
    calls=[]
    def get(url, **kwargs):
        calls.append(url)
        assert dest.read_bytes()==b'old complete image'
        if len(calls)==1:return Reply([b'partial', requests.exceptions.ChunkedEncodingError('cut')])
        return Reply([b'new ', b'complete image'], 18)
    monkeypatch.setattr(providers.requests,'get', get)
    providers.download('https://asset.example/nora.png', dest)
    assert calls==['https://asset.example/nora.png']*2
    assert dest.read_bytes()==b'new complete image'
    assert list(tmp_path.iterdir())==[dest]


def test_exhausted_get_then_resume_never_submits_paid_generation(tmp_path, monkeypatch, no_sleep):
    (tmp_path/'episode').mkdir()
    state=State(tmp_path/'episode')
    result={'images':[{'url':'https://asset.example/already-paid.png'}]}
    state.take('nora_profile_left_00').update(status='succeeded', result=result, request_id='paid-request-saved')
    state.save()
    cfg=SimpleNamespace(dry_run=False,image_resolution='1K',fal_image_pro_model='pro',fal_image_t2i_model='image',fal_image_model='edit')
    post=Mock(side_effect=AssertionError('must not submit'))
    monkeypatch.setattr(providers.requests,'post',post)
    get=Mock(side_effect=lambda *a,**k:Reply([b'broken',requests.exceptions.ChunkedEncodingError('cut')]))
    monkeypatch.setattr(providers.requests,'get',get)
    fal=DurableFal(cfg,lambda _:None,state,Mock(),None)
    dest=tmp_path/'nora.png'
    with pytest.raises(requests.exceptions.ChunkedEncodingError):
        providers.gen_image(fal,'nora_profile_left_00','Same saved prompt',dest,[],'3:4',True,'Nora')
    assert not dest.exists() and get.call_count==3
    # Simulate worker recreation from its durable saved state.
    restored=State(tmp_path/'episode')
    fal=DurableFal(cfg,lambda _:None,restored,Mock(),None)
    monkeypatch.setattr(providers.requests,'get',lambda *a,**k:Reply([b'complete'],8))
    path,take=providers.gen_image(fal,'nora_profile_left_00','Same saved prompt',dest,[],'3:4',True,'Nora')
    assert path.read_bytes()==b'complete'
    assert take['request_id']=='paid-request-saved'
    post.assert_not_called()
    fal.budget.reserve.assert_not_called()
    assert not list(tmp_path.glob('.download-*'))


def test_incomplete_content_length_does_not_publish_partial_file(tmp_path,monkeypatch,no_sleep):
    monkeypatch.setattr(providers.requests,'get',lambda *a,**k:Reply([b'short'],100))
    dest=tmp_path/'asset.png'
    with pytest.raises(requests.exceptions.ChunkedEncodingError):providers.download('https://asset.example/file',dest)
    assert not dest.exists() and not list(tmp_path.glob('.download-*'))


def test_permanent_404_is_not_retried(tmp_path,monkeypatch,no_sleep):
    response=requests.Response();response.status_code=404
    get=Mock(side_effect=requests.exceptions.HTTPError(response=response))
    monkeypatch.setattr(providers.requests,'get',get)
    with pytest.raises(requests.exceptions.HTTPError):providers.download('https://asset.example/file',tmp_path/'asset.png')
    assert get.call_count==1
