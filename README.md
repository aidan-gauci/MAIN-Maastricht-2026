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

Open the URL printed by Vite. The default uses fourteen fictional articles and makes no provider calls. Install eSpeak NG for offline audio, or use Windows with an installed system voice. On macOS or Linux, use the virtual environment's `bin/python`. If synthesis fails, the transcript stays available and you can retry audio. For a single local server, run `npm run build` in `frontend` and restart the backend to serve the built app on port 8000.

## Prepare the event corpus

Set `LIQUID_CORPUS=event` in the backend and import terminals. Point the CLI at the organizers' ZIP, a directory of JSONL feeds, or one JSONL file. Stop the server while replacing or purging the corpus. The ZIP is read directly, and article bodies stay out of SQLite. The demo database and event database are separate.

```powershell
$env:LIQUID_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app import "C:/path/liquid-briefing-data.zip"
./.venv/Scripts/python.exe -m backend.app costs
./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Use `--limit 20` for a sample and `--official PATH` to append public releases in the normalized JSONL format. See `backend/demo/articles.jsonl` for that format. Event preparation without providers is a heuristic baseline. Review its clusters and evidence before using it in a live demo. The evaluation command accepts `{"stories":[{"label":"Example","article_ids":["id-1","id-2"],"values":[3.5]}]}` and reports split clusters, extra articles, and missing numeric values. Convert the organizer key to that shape and run `python -m backend.app evaluate PATH` separately from preparation.

Set `LIQUID_PROFILES` to an untracked JSON file matching `backend/demo/profiles.json` to load the four official profile cards when they arrive. The wildcard uses prepared topic chips, three expertise levels, durations of 3, 5, or 8 minutes, and a list of supported output languages. Duration is a target and depends on how much verified material is available.

## Live providers and costs

Install `backend/requirements-live.txt`. Set `LIQUID_MODE=live`, `ALLOW_EXTERNAL_PROVIDER_DATA=1`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` in the backend environment. Import before launching the server. Only use live event processing if the organizers permit those provider transfers, and pass `--allow-provider-processing` to the event import in that case. Start with English and set `LIQUID_MULTILINGUAL=1` after validating English narration and German and Dutch outputs.

The application records token usage for OpenAI and submitted text characters for ElevenLabs. Set the rates for your selected models and account to calculate USD estimates:

| Variable | Unit |
| --- | --- |
| `OPENAI_EMBEDDING_INPUT_USD_PER_MILLION_TOKENS` | USD per million embedding input tokens |
| `OPENAI_INPUT_USD_PER_MILLION_TOKENS` | USD per million text input tokens |
| `OPENAI_OUTPUT_USD_PER_MILLION_TOKENS` | USD per million text output tokens |
| `ELEVENLABS_USD_PER_1000_CHARS` | USD per 1,000 submitted characters |

Use a fresh event data directory for a clean measurement because the cost view accumulates calls, including retries. The 10x preparation projection assumes the same article mix, model routing, and unit rates. It does not multiply the cost of one listener's briefing. Missing rates leave the USD estimate unavailable.

## Demo and cleanup

Generate two distinct profiles, audit three spoken claims against their source articles, generate a wildcard live, and open the evidence and cost views. For the presentation, show the full corpus measurement and 10x estimate. Pitch a newsroom publisher as the buyer and show how editorial review fits between batch preparation and audio delivery.

Run the fixture checks with `python -m unittest discover -s backend/tests`. To inspect just the expected fixture clusters and values, import the fictional corpus and run `python -m backend.app evaluate backend/demo/answer-key.json`.

After the event, stop the backend, set `LIQUID_CORPUS=event`, and run `python -m backend.app purge-event --confirm`. This purges the app databases and audio for both event modes. Also delete the original ZIP and any other copies you downloaded, extracted, exported, or backed up. Keep the licensed data and derivatives out of Git and shared storage.
