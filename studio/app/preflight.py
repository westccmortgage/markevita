"""Local admission checks. These never call a generation API or change settings."""
import math
import os
import shutil

from serial.costs import PRICE
from serial.fal_auth import key_problem


DEPENDENCIES = {
    'direction': ('intake',), 'keyframes': ('direction', 'references'),
    'video': ('keyframes',), 'voice': ('intake',),
    'lipsync': ('video', 'voice'), 'assemble': ('lipsync',),
    'qa': ('assemble',), 'deliver': ('qa',),
}


def problems(cfg, stages, pkg):
    """Collect deterministic blockers together, rather than fail one at a time."""
    errors = []
    unknown = set(stages) - {'intake', 'direction', 'references', 'keyframes', 'video', 'voice', 'lipsync', 'assemble', 'qa', 'deliver'}
    if unknown:
        errors.append('Choose production stages from the episode form; publishing is separate.')
    native = getattr(cfg, 'native_dialogue', False)
    needed = [s for s in stages if not (native and s in ('voice', 'lipsync'))]
    missing = cfg.missing_for(needed)
    if not cfg.r2_configured:
        missing.append('R2 storage credentials')
    if missing:
        errors.append('Configure before production: ' + ', '.join(dict.fromkeys(missing)))
    if {'references', 'keyframes', 'video', 'lipsync'}.intersection(needed) and cfg.fal_key:
        if error := key_problem(cfg.fal_key):
            errors.append(error)
    # The current adapters and prices implement these exact endpoints. A new
    # model needs its own payload/price adapter, not just a changed env value.
    supported = [({'references', 'keyframes'}, 'FAL_IMAGE_MODEL', cfg.fal_image_model, 'fal-ai/nano-banana-2/edit'),
                 ({'video'}, 'FAL_VIDEO_MODEL', cfg.fal_video_model, 'fal-ai/veo3.1/fast/image-to-video')]
    if not native:
        supported.append(({'lipsync'}, 'FAL_LIPSYNC_MODEL', cfg.fal_lipsync_model, 'fal-ai/sync-lipsync/v2'))
    for uses, name, value, expected in supported:
        if uses.intersection(stages) and value != expected:
            errors.append(f'{name}: this production adapter requires {expected}. Other models need a separate integration.')
    if {'references', 'keyframes'}.intersection(stages):
        # Reference Pro pricing in this release covers 1K/2K only.
        allowed = ('1K', '2K') if 'references' in stages else ('1K', '2K', '4K')
        if cfg.image_resolution not in allowed:
            errors.append('IMAGE_RESOLUTION: supported with current reference pricing: ' + ', '.join(allowed))
    if 'video' in stages:
        if cfg.video_resolution not in ('720p', '1080p', '4k'):
            errors.append('VIDEO_RESOLUTION: choose 720p, 1080p or 4k.')
        if pkg.series['format']['aspect_ratio'] not in ('16:9', '9:16'):
            errors.append('Video aspect ratio: the configured video model supports 16:9 or 9:16.')
    if not native and 'lipsync' in stages and cfg.lipsync_variant not in ('lipsync-2', 'lipsync-2-pro'):
        errors.append('LIPSYNC_VARIANT: choose lipsync-2 or lipsync-2-pro.')
    if cfg.provider_input_mode not in ('r2_presigned', 'fal_storage'):
        errors.append('PROVIDER_INPUT_MODE: choose r2_presigned or fal_storage.')
    if set(stages).intersection({'video', 'voice', 'lipsync', 'assemble', 'qa'}):
        for executable in ('ffmpeg', 'ffprobe'):
            if not shutil.which(executable):
                errors.append(f'{executable}: required on the production server before starting.')
    for name, amount in PRICE.items():
        if name.startswith('_'):
            continue
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
            errors.append(f'PRICE_{name.upper()}: enter a finite, non-negative price.')
    budget = pkg.limits(cfg)['budget']
    if not math.isfinite(budget) or budget <= 0:
        errors.append('Episode budget: enter a finite amount greater than zero.')
    return errors


def voice_problems(cfg, stages, pkg, episode_id):
    if getattr(cfg, 'native_dialogue', False) or 'voice' not in stages:
        return []
    speakers = {d['speaker'] for s in pkg.load_episode(episode_id)['scenes'] for d in s.get('dialogue', [])}
    missing = [c for c in sorted(speakers)
               if not os.environ.get((pkg.characters.get(c, {}).get('voice') or {}).get('voice_env', ''), '').strip()
               and not cfg.voice_ids.get(c, '').strip()]
    errors = []
    if missing:
        errors.append('Assign ElevenLabs voices for ' + ', '.join(missing) + ', or choose Native scene audio.')
    if speakers and PRICE['elevenlabs_per_1k_chars_estimate'] <= 0:
        errors.append('Set PRICE_ELEVENLABS_PER_1K_CHARS_ESTIMATE for your plan, or choose Native scene audio.')
    return errors


def recovery_problems(stages, state):
    """Check checkpoint prerequisites before spending on any earlier stage."""
    from .live_providers import _known_refusal, _UNCERTAIN
    errors = []
    done = {name for name, value in state.data.get('stages', {}).items() if value == 'done'}
    for stage in stages:
        if stage in done:
            continue
        missing = set(DEPENDENCIES.get(stage, ())) - done - set(stages)
        if missing:
            errors.append(f'{stage}: include the unfinished prerequisite stages: ' + ', '.join(sorted(missing)))
    unknown = [tid for tid, take in state.data.get('takes', {}).items()
               if take.get('status') in _UNCERTAIN and not take.get('request_id') and not _known_refusal(take)]
    if unknown:
        errors.append('A provider submission needs reconciliation; do not force or repeat generation.')
    if any(rec.get('status') != 'succeeded' for rec in state.data.get('paid_operations', {}).values()):
        errors.append('A paid request was interrupted before its response was saved. Reconcile it with the provider before another attempt.')
    return errors
