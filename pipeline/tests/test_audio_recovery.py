"""Real ffmpeg checks for soundtrack repair; no provider requests are allowed."""
import copy
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serial import media, providers
from serial.costs import Budget
from serial.pipeline import Pipeline
from serial.state import State, sha256


def video_file(path, expression="0.2*sin(2*PI*440*t)", seconds=32):
    subprocess.run([
        'ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=blue:s=160x284:r=24:d={seconds}',
        '-f', 'lavfi', '-i', f'aevalsrc={expression}:s=48000:d={seconds}',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-b:a', '192k', '-shortest', str(path),
    ], check=True)
    return path


def video_packets(path):
    return subprocess.run([
        'ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v:0', '-c:v', 'copy', '-f', 'hash', '-',
    ], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def audio_pipeline(tmp_path, monkeypatch):
    p = object.__new__(Pipeline)
    p.work, p.out = tmp_path / 'work', tmp_path / 'out'
    p.work.mkdir(); p.out.mkdir()
    mdir = p.out / 'masters' / 'v1'; mdir.mkdir(parents=True)
    master = video_file(mdir / 'episode.mp4')
    media.poster_frame(master, 1, mdir / 'poster.jpg')
    media.write_srt([{'start': 1, 'end': 3, 'text': 'Original dialogue.'}], mdir / 'episode.srt')
    (mdir / 'manifest.json').write_text(json.dumps({'master_version': 'v1', 'master_sha256': sha256(master),
        'scenes': [{'video_take': 'saved_take'}], 'spent_usd': 13.30}))
    (mdir / 'metadata.json').write_text(json.dumps({'master_version': 'v1', 'language': 'en-US', 'title': 'Original episode'}))
    p.state = State(tmp_path)
    scene = {'scene_id': 'sc01', 'source_scene_id': 'sc01', 'dialogue': [{'text': 'Original dialogue.'}]}
    p.state.data.update(
        master_version=1, master_dir=str(mdir), spent_usd=13.30,
        episode={'limits': {'min_sec': 30, 'max_sec': 40, 'budget': 50}, 'width': 160, 'height': 284,
                 'total_seconds': 32, 'scenes': [scene], 'cliffhanger': {'scene_id': 'sc01'}},
        takes={'saved_take': {'status': 'succeeded', 'endpoint': 'offline', 'request_id': 'already-paid',
                             'checksum': sha256(master), 'estimated_cost': 13.30, 'scene_id': 'sc01'}},
        stages={name: 'done' for name in ['intake', 'direction', 'references', 'keyframes', 'video', 'assemble']},
    )
    p.cfg = SimpleNamespace(fal_video_model='offline')
    p.force, p.regen, p.log = set(), 2, lambda message: None
    p.budget = Budget(50, p.state)
    p.state.save()
    def forbidden(*a, **kw):
        pytest.fail('Audio repair must not request image, video, voice or lipsync generation')
    for name in ['gen_image', 'gen_video', 'tts', 'lipsync']:
        monkeypatch.setattr(providers, name, forbidden)
    return p, mdir


def test_failed_audio_master_recovers_without_changing_video_takes_or_subtitles(audio_pipeline):
    p, original = audio_pipeline
    original_files = {f.name: f.read_bytes() for f in original.iterdir()}
    takes = copy.deepcopy(p.state.data['takes'])
    assert media.loudness(original / 'episode.mp4')['integrated_lufs'] < -16.5
    p.stage_qa()
    repaired = Path(p.state.data['master_dir'])
    assert repaired.name == 'v2' and p.state.stage_done('qa')
    assert p.state.data['takes'] == takes and p.budget.spent == 13.30
    assert {f.name: f.read_bytes() for f in original.iterdir()} == original_files
    assert (repaired / 'episode.srt').read_bytes() == original_files['episode.srt']
    assert video_packets(repaired / 'episode.mp4') == video_packets(original / 'episode.mp4')
    assert abs(media.duration(repaired / 'episode.mp4') - 32) < 0.2
    manifest = json.loads((repaired / 'manifest.json').read_text())
    assert manifest['master_sha256'] == sha256(repaired / 'episode.mp4')
    assert manifest['audio_repair']['source_master_sha256'] == sha256(original / 'episode.mp4')
    old_report = json.loads((p.out / 'qa' / 'v1' / 'report.json').read_text())
    assert {c['check'] for c in old_report['checks'] if not c['pass']} == {'loudness_target'}
    report = json.loads((p.out / 'qa' / 'v2' / 'report.json').read_text())
    assert report['pass'] and -16.5 <= report['loudness']['integrated_lufs'] <= -11.5
    assert report['loudness']['true_peak_dbtp'] <= -0.5
    # A restarted worker uses the saved correction and does not create v3.
    p.state = State(p.state.path.parent)
    p.stage_qa()
    assert p.state.data['master_version'] == 2


def test_other_qa_failures_still_block_delivery(audio_pipeline):
    p, original = audio_pipeline
    p.state.data['episode']['width'] = 180
    with pytest.raises(RuntimeError, match='resolution'):
        p.stage_qa()
    assert p.state.data['master_version'] == 1
    assert not p.state.stage_done('qa') and p.state.data['status'] == 'failed_qa'


def test_unsuccessful_correction_is_not_repeated_on_resume(audio_pipeline, monkeypatch):
    p, original = audio_pipeline
    monkeypatch.setattr(media, 'loudness', lambda _: {'integrated_lufs': -16.7, 'lra': 2, 'true_peak_dbtp': -1.5})
    for _ in range(2):
        with pytest.raises(RuntimeError, match='loudness_target'):
            p.stage_qa()
        p.state = State(p.state.path.parent)
    assert p.state.data['master_version'] == 2 and not p.state.stage_done('qa')


def test_silent_audio_is_not_declared_normalized(tmp_path):
    source = video_file(tmp_path / 'silent.mp4', expression='0', seconds=4)
    with pytest.raises(RuntimeError, match='silent or invalid'):
        media.loudnorm(source, tmp_path / 'normalized.mp4')
    assert not (tmp_path / 'normalized.mp4').exists()


def test_two_pass_handles_large_changes_in_volume(tmp_path):
    source = video_file(tmp_path / 'varying.mp4', expression=r'if(lt(t\,16)\,0.03\,0.3)*sin(2*PI*440*t)')
    dest = media.loudnorm(source, tmp_path / 'normalized.mp4')
    measured = media.loudness(dest)
    assert abs(measured['integrated_lufs'] - (-14)) < 0.5
    assert measured['true_peak_dbtp'] <= -0.5
    assert video_packets(dest) == video_packets(source)


def test_failed_audio_encode_keeps_previous_destination(tmp_path, monkeypatch):
    source = video_file(tmp_path / 'source.mp4', seconds=4)
    dest = tmp_path / 'existing.mp4'; dest.write_bytes(b'previous complete file')
    real_run = media._run
    def interrupted(cmd):
        if '-c:a' in cmd:
            Path(cmd[-1]).write_bytes(b'partial encode')
            raise subprocess.CalledProcessError(1, 'ffmpeg')
        return real_run(cmd)
    monkeypatch.setattr(media, '_run', interrupted)
    with pytest.raises(subprocess.CalledProcessError):
        media.loudnorm(source, dest)
    assert dest.read_bytes() == b'previous complete file'
    assert not list(tmp_path.glob('.loudnorm-*'))
