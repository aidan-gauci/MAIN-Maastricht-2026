# The Liquid Briefing

The Liquid Briefing turns prepared business news into personalized audio. Choose a listener, generate a briefing, and open any transcript segment to inspect its sources. The evidence and cost views support the hackathon demo.

## Run locally

Use Python 3.11 or newer and Node.js 22.12 or newer. From the repository root, start the backend:

```powershell
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open the URL printed by Vite. The default uses fourteen fictional articles, needs no credentials, and makes no provider calls. On first start the backend prepares the fixture automatically. Install eSpeak NG for offline audio, or use Windows with an installed system voice. On macOS or Linux, use the virtual environment's `bin/python`. If synthesis fails or takes longer than `LIQUID_SPEECH_TIMEOUT_SECONDS` (default 60), the briefing is still saved with its transcript and `audio_status: "unavailable"`; use the retry-audio control (`POST /api/briefings/{id}/audio`). Retrying also re-renders audio whose file was deleted. For a single local server, run `npm run build` in `frontend` and restart the backend to serve the built app on port 8000.

The backend does not read `.env` files. Set variables in each PowerShell terminal with `$env:NAME = "value"`. `LIQUID_MODE`, `LIQUID_CORPUS`, and `LIQUID_DATA_DIR` must match between the server and CLI terminals, because each combination uses its own database under `.liquid-data/<corpus>/<mode>`. [.env.example](.env.example) lists every variable without values. Keep keys in the backend terminal only.

## Demo runbook

Run these from the repository root in PowerShell. Stop the backend before reset, import, or cleanup.

1. **Preflight.** Run the tests, then check the fixture clusters:

   ```powershell
   ./.venv/Scripts/python.exe -m unittest discover -s backend/tests
   ./.venv/Scripts/python.exe -m backend.app import
   ./.venv/Scripts/python.exe -m backend.app evaluate backend/demo/answer-key.json
   ```

2. **Reset the fictional demo.** This deletes generated fixture briefings, audio, and usage for both modes. The next start or `import` rebuilds them. You can run it repeatedly.

   ```powershell
   ./.venv/Scripts/python.exe -m backend.app reset-demo --confirm
   ```

3. **Run.** Start the backend and frontend as above. Generate briefings, open the evidence view, and retry audio if it is unavailable.

4. **Costs.** `./.venv/Scripts/python.exe -m backend.app costs` prints the same report as `GET /api/costs`. Demo mode reports $0 throughout.

5. **Cleanup.** See *Demo and cleanup* below.

## Prepare the event corpus

Set `LIQUID_CORPUS=event` in the backend and import terminals. Point the CLI at the organizers' ZIP, a directory of JSONL feeds, or one JSONL file. Feeds are found at any depth, with any `.jsonl` capitalization, and macOS `__MACOSX/` and `._` entries are skipped. Stop the server while replacing or purging the corpus. The ZIP is read directly, and article bodies stay out of SQLite. The demo database and event database are separate.

Import is repeatable. Each run replaces the corpus in one transaction, and preparation usage from earlier imports is replaced with it. If reading or preparing fails, the CLI exits with code 2, names the file and line or the repeated article IDs, and keeps the previous corpus. Earlier briefings stay viewable after a re-import. In live mode, a failed provider extraction also keeps the previous corpus. Pass `--allow-offline-fallback` to store the local heuristic clusters instead.

Keep the organizer files outside the repository. `.gitignore` also ignores `*.zip`, `*.jsonl` (except `backend/demo`), `__MACOSX/`, audio files, `*.local.json`, `/data/`, `/event/`, and `.liquid-data/`, as a safety net.

```powershell
$env:LIQUID_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app import "C:/path/liquid-briefing-data.zip"
./.venv/Scripts/python.exe -m backend.app costs
./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Use `--limit 20` for a sample and `--official PATH` to append public releases in the normalized JSONL format. See `backend/demo/articles.jsonl` for that format. Event preparation without providers is a heuristic baseline. Review its clusters and evidence before using it in a live demo. The evaluation command accepts `{"stories":[{"label":"Example","article_ids":["id-1","id-2"],"values":[3.5]}]}` and reports split clusters, extra articles, and missing numeric values. Convert the organizer key to that shape and run `python -m backend.app evaluate PATH` separately from preparation.

Set `LIQUID_PROFILES` to an untracked JSON file matching `backend/demo/profiles.json`, for example `profiles.local.json`, to load the four official profile cards when they arrive. The wildcard uses prepared topic chips, three expertise levels, durations of 3, 5, or 8 minutes, and a list of supported output languages. Duration is a target and depends on how much verified material is available.

## Live providers and costs

Install `backend/requirements-live.txt`. Set `LIQUID_MODE=live`, `ALLOW_EXTERNAL_PROVIDER_DATA=1`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` in the backend environment. Import before launching the server. Only use live event processing if the organizers permit those provider transfers, and pass `--allow-provider-processing` to the event import in that case. Start with English and set `LIQUID_MULTILINGUAL=1` after validating English narration and German and Dutch outputs.

The application records token usage for OpenAI and submitted text characters for ElevenLabs. Set the rates for your selected models and account to calculate USD estimates:

| Variable | Unit |
| --- | --- |
| `OPENAI_EMBEDDING_INPUT_USD_PER_MILLION_TOKENS` | USD per million embedding input tokens |
| `OPENAI_INPUT_USD_PER_MILLION_TOKENS` | USD per million text input tokens |
| `OPENAI_OUTPUT_USD_PER_MILLION_TOKENS` | USD per million text output tokens |
| `ELEVENLABS_USD_PER_1000_CHARS` | USD per 1,000 submitted characters |

Costs are split into two figures:
- `preparation_usd` covers ingestion and story extraction for the corpus currently loaded. Re-importing replaces it instead of adding to it.
- `per_briefing_usd` is script spend averaged over saved briefings, plus speech spend averaged over successful audio renders. Failed audio therefore does not dilute it, and a successful retry counts as one render.

USD figures are calculated from the recorded tokens and characters with the rates set when the report is built, so you can set or correct rates after a run. If a rate is missing for a stage that was used, the matching figure is unavailable. The 10x preparation projection assumes the same article mix, model routing, and unit rates. It does not multiply the cost of one listener's briefing.

## Demo and cleanup

Generate two distinct profiles, audit three spoken claims against their source articles, generate a wildcard live, and open the evidence and cost views. For the presentation, show the full corpus measurement and 10x estimate. Pitch a newsroom publisher as the buyer and show how editorial review fits between batch preparation and audio delivery.

Run the fixture checks with `./.venv/Scripts/python.exe -m unittest discover -s backend/tests`. To inspect just the expected fixture clusters and values, import the fictional corpus first, then run `./.venv/Scripts/python.exe -m backend.app evaluate backend/demo/answer-key.json`.

After the event, stop the backend and purge the event data:

```powershell
$env:LIQUID_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app purge-event --confirm
```

This removes the app databases, audio, and the folders the app created, for both event modes, under the current `LIQUID_DATA_DIR`. It is safe to repeat. If you used other data directories, set `$env:LIQUID_DATA_DIR` to each one and run it again. If the backend is still running, the command stops with "Stop the backend" instead of half-deleting. Also delete the original ZIP, the `LIQUID_PROFILES` file, and any other copies you downloaded, extracted, exported, or backed up. Keep the licensed data and derivatives out of Git and shared storage.

If the backend reports a database error after you update the code, run `reset-demo --confirm` (fixture) or `purge-event --confirm` (event) and import again.
