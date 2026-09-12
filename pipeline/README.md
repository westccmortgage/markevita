# AI Series Pipeline (content-agnostic)

Автономный конвейер: **series package** (JSON-библия + бриф серии) → референсы → кадры → видео → голос → lipsync → сборка → QA → R2 → approval → публикация.
Пайплайн не знает сюжета: любой пакет по контракту `docs/SERIES_PACKAGE.md` запускается без правок кода.
Статический сайт в корне репозитория не затрагивается.

## Установка

```bash
cd pipeline
pip install -r requirements.txt      # + ffmpeg в PATH
cp .env.example .env                 # ключи только здесь, никогда в пакет и не в git
```

## Запуск

```bash
python run_episode.py --validate ../series/<id>                          # бесплатно: схемы, континьюити, ledger, cliffhanger
python run_episode.py --series ../series/<id> --episode s01e01           # mock: вся цепочка на заглушках до deliver
python run_episode.py --series ../series/<id> --approve references --by "Anatoliy"
python run_episode.py --series ../series/<id> --episode s01e01 --approve publish --by "Anatoliy"
python run_episode.py --series ../series/<id> --episode s01e01 --stages publish
```

Платный режим (`--live`) включается только при трёх условиях одновременно: флаг `--live`, `PIPELINE_ALLOW_PAID=true` в `.env`,
`approval.status: "approved"` в `series.json`. Ни одна из этих вещей не включена.

## Стадии

| Стадия | Провайдер | Что делает |
|---|---|---|
| intake | — | схемы, кросс-проверки, split двух говорящих, ledger знаний/отношений, cliffhanger, проекция бюджета |
| direction | Claude (только если нет production_prompts.json) | промпты кадра/движения + continuity plan |
| references | Nano Banana Pro / Nano Banana 2 edit | пакет референсов на версию библии: headshots, expressions, full-body на каждый вариант гардероба, локации, пропсы. Требует approval |
| keyframes | Nano Banana 2 edit + Claude vision QC | первый кадр каждого клипа, до 2 повторов |
| video | Veo 3.1 Fast i2v (1080p, без аудио) + QC по 3 кадрам | клип 4/6/8 с |
| voice | ElevenLabs eleven_v3 | реплика за репликой, phone-fx, time-fit в клип (atempo ≤1.15) |
| lipsync | Sync Lipsync 2.0 | только видимый говорящий; VO подмешивается |
| assemble | ffmpeg | нормализация, склейка, room tone/music, srt/burn-in, loudnorm −14 LUFS, master, poster, metadata, manifest |
| qa | ffmpeg | длительность, разрешение, декодирование, чёрные кадры, громкость, retry-лимит, бюджет, provenance, субтитры, cliffhanger |
| deliver | R2 | masters/qa/takes/audio/ledger в private namespace |
| publish | R2 | требует approval; копирует в `/public/`. Instagram: feature-flag выключен |

Каждый платный вызов = take: endpoint, request_id, промпт, параметры, checksum входов/выхода, оценка/факт стоимости, QC. Падение после submit не создаёт второго запроса: результат забирается по request_id. Повторный запуск ничего не пересчитывает. `--force <scene_id>` создаёт новые takes (старые не удаляются) и помечает их `forced_by_operator`.

## Тесты

```bash
python -m pytest tests -q     # ~5 мин: валидация, split, ledger, cliffhanger, континьюити между сериями, e2e mock с гейтами, idempotency, resume после падения, бюджет, live-guard
```

## Что в корне репо устарело

`docs/CHARACTER_BIBLE.md`, `docs/PIPELINE_SPEC.md` (разделы про сюжет), `episodes/ep01/brief.json`, `MARKEVITA_AI_SERIES_ENGINEER_HANDOFF.md` (creative-часть) — прежний креатив, пайплайн их не читает. Технические решения из спецификации (провайдеры, R2, лимиты, секреты) остаются в силе и реализованы здесь. Не удалял: см. `IMPLEMENTATION_NOTES.md`.
