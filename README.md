# Maze

**Find your way through the news.** Maze turns prepared business news into short, personalized audio briefings. Every spoken figure is traced to its source. Choose a listener, generate a briefing, and open any transcript segment, or the Traces view, to follow each claim back to the reporting. The evidence and cost views support the hackathon demo.

Configuration and storage use neutral, descriptive names rather than the product name: `BRIEFING_*` environment variables, the `.briefing-data` folder, `briefing-*` browser storage keys, the `X-Scheduler-Secret` header and `briefing-*` n8n workflows. A rename of the product does not touch deployments or saved settings.

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

Open the URL printed by Vite. The default uses fourteen fictional articles, needs no credentials, and makes no provider calls. On first start the backend prepares the fixture automatically. Install eSpeak NG for offline audio, or use Windows with an installed system voice. On macOS or Linux, use the virtual environment's `bin/python`. If synthesis or audio assembly fails, the briefing is still saved with its transcript and `audio_status: "unavailable"`. Use the retry-audio control (`POST /api/briefings/{id}/audio`). Retrying also re-renders audio whose file was deleted. Set `BRIEFING_SPEECH_TIMEOUT_SECONDS` (default 60) to limit synthesis time per clip. For a single local server, run `npm run build` in `frontend` and restart the backend to serve the built app on port 8000.

The backend does not read `.env` files. Set variables in each PowerShell terminal with `$env:NAME = "value"`. `BRIEFING_MODE`, `BRIEFING_CORPUS`, and `BRIEFING_DATA_DIR` must match between the server and CLI terminals, because each combination uses its own database under `.briefing-data/<corpus>/<mode>`. [.env.example](.env.example) lists every variable without values. Keep keys in the backend terminal only.

## Demo runbook

Run these from the repository root in PowerShell. Stop the backend before reset, import, or cleanup.

1. **Preflight.** Run the tests, then import the fixture:

   ```powershell
   ./.venv/Scripts/python.exe -m unittest discover -s backend/tests
   ./.venv/Scripts/python.exe -m backend.app import
   ```

2. **Reset the fictional demo.** This deletes generated fixture briefings, audio, and usage for both modes. The next start or `import` rebuilds them. You can run it repeatedly.

   ```powershell
   ./.venv/Scripts/python.exe -m backend.app reset-demo --confirm
   ```

3. **Run.** Start the backend and frontend as above. Generate briefings, open the evidence view, and retry audio if it is unavailable. The app remembers facts a listener heard in the last seven days, and the fixture has only ten stories. After about three briefings in one browser, repeated requests return off-topic filler or "No sourced stories match this profile". Run `reset-demo --confirm` (with the backend stopped) or clear the site's local storage between rehearsals.

4. **Costs.** `./.venv/Scripts/python.exe -m backend.app costs` prints the same report as `GET /api/costs`. Demo mode reports $0 throughout.

5. **Cleanup.** See *Demo and cleanup* below.

### If a live provider fails during the demo

- **Speech fails (ElevenLabs).** The briefing still opens with its transcript, sources and traces, and the player offers **Retry audio**. Any briefing played earlier replays from cached audio at no cost, so rehearse the demo briefings first.
- **Narration or extraction fails (OpenAI).** A new listener's briefing uses prepared text and needs no OpenAI call, so start the demo with a fresh browser profile. Corpus preparation is done before the demo, never live.
- **Both fail or the network drops.** Stop the server and restart it in a new terminal without the key file, so `BRIEFING_MODE` and `BRIEFING_CORPUS` are unset. The app runs on the fictional fixture with the offline system voice, makes no provider calls, and shows the same Listen, Evidence, Traces and Costs views.

To run the browser smoke test, activate the backend virtual environment, then run `npx playwright install chromium` and `npm run test:smoke` in `frontend`. The test builds the frontend and starts a local fixture server with simulated speech. Set `PYTHON` to the virtual environment’s Python executable if needed.

## Prepare the event corpus

Set `BRIEFING_CORPUS=event` in the backend and import terminals. Point the CLI at the organizers' ZIP, a directory of JSONL feeds, or one JSONL file. Feeds are found at any depth, with any `.jsonl` capitalization, and macOS `__MACOSX/` and `._` entries are skipped. Stop the server while replacing or purging the corpus. The ZIP is read directly, and article bodies stay out of SQLite. The demo database and event database are separate.

Import is repeatable. Each run replaces the corpus in one transaction, and preparation usage from earlier imports is replaced with it. If reading or preparing fails, the CLI exits with code 2, names the file and line or the repeated article IDs, and keeps the previous corpus. Earlier briefings stay viewable after a re-import. In live mode, a failed provider extraction also keeps the previous corpus. Pass `--allow-offline-fallback` to store the local heuristic clusters instead. A provider reply that is truncated or not valid JSON is not such a failure: that batch is retried as two halves, down to single articles, and an article that still fails is extracted with the local heuristic. The import summary reports these as `heuristic_fallback_articles`.

Keep the organizer files outside the repository. `.gitignore` also ignores `*.zip`, `*.jsonl` (except `backend/demo`), `__MACOSX/`, audio files, `*.local.json`, `/data/`, `/event/`, and `.briefing-data/`, as a safety net.

```powershell
$env:BRIEFING_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app import "C:/path/event-data.zip"
./.venv/Scripts/python.exe -m backend.app costs
./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Use `--limit 20` for a sample and `--official PATH` to append public releases in the normalized JSONL format. See `backend/demo/articles.jsonl` for that format. Event preparation without providers is a heuristic baseline. Review its clusters and evidence before using it in a live demo. No organizer answer key was provided, so grouping quality is judged by reading the clusters and spot-checking facts against their source articles.

Set `BRIEFING_PROFILES` to an untracked JSON file matching `backend/demo/profiles.json`, for example `profiles.local.json`, to load your own profile cards in place of the four built-in ones (everyday economics, markets professional, policy watcher, and a German everyday listener). Choosing a profile sets the output language to the profile's own. The wildcard uses topic chips, three expertise levels, a length of 3, 5, or 8 stories (about 1, 2, or 3 minutes of audio), and the supported output languages. On a large corpus the chips are the subject areas that several speakable stories report on, then the most common tagged subjects, 36 at most.

### Labels in the listeners' other languages

A figure is named in the words of the article that reports it, and a story is announced by its lead article's headline. For listeners in another language, a live import translates those short labels with the small model, one request per story (`label_translation` in the cost report): the headline, and each measure name. Figures never pass through it. Every translation is checked before it is kept: it must repeat the numbers of the original and add none, a measure name must hold no number and no advice word, and its length must be in proportion. A label that fails is dropped and the story is narrated in that language as it would be without it. A translated headline is marked as such in the transcript, with the original beside it.

For a corpus imported before this step existed, run it on its own. It asks only for labels that are missing, so it can be repeated:

```powershell
$env:BRIEFING_MODE = "live"; $env:BRIEFING_CORPUS = "event"; $env:ALLOW_EXTERNAL_PROVIDER_DATA = "1"
./.venv/Scripts/python.exe -m backend.app translate-labels
```

### Regrouping an imported corpus, and event threads

Grouping decides which articles are one story. It matches translations (a German or Dutch article within minutes of its English original, by shared figures; short articles with few figures need the same outlet, five minutes and shared headline words), treats one outlet's lookalike headlines as separate pieces unless their text is the same, and, in live mode, merges two outlets' reports of one event by embedding, figures and time (digests never merge). A multi-day saga is not merged. Its stories form an **event thread** instead: stories within five days that name the same distinctive person, company or ticker in their English headlines and are about the same thing (embedding cosine 0.65 or more; without embeddings, four shared headline words). Common names, digests and names spread over months do not make threads. Each story keeps its own facts and gains `thread_id` and `thread_title` (visible in `/api/evidence`), and a briefing plays at most one story per thread.

`regroup` applies the current grouping rules to an already imported corpus without any provider call, so a rule change does not repeat the paid extraction. Facts are tied to their article, so each new story takes the union of its members' stored facts. Conflict labels, thread markers, summaries and the story ID are recomputed, and unchanged stories are kept as they are. The article text is not stored, so point the command at the same files the corpus was imported from. It follows the same rules as `import` for the corpus, mode and data directory, and, like `import`, stop the server first. Everything is replaced in one transaction and any failure keeps the previous corpus. Briefing history stays, but feedback on a story whose members changed no longer matches its new ID. In live mode the stored story embeddings merge cross-outlet reports again; one vector is kept per story, so a story that was a merge keeps one vector for both halves if the new rules split it.

```powershell
$env:BRIEFING_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app regroup "C:/path/event-data.zip"
```

It prints the stories before and after, how many were merged or split, the cross-outlet merges and the threads.

`evaluate-grouping PATH [--embeddings vectors.json] [--probe WORD]` is an offline check that reads the feeds only. It scores translation matching against the organizers' pairs (the same author and outlet family within 15 minutes, taking the nearest English piece; a team byline counts when the figures match), and reports group sizes, multi-day groups, one outlet's repeated stories, cross-outlet merges (when given a JSON mapping of article ID to story vector) and event threads. `--probe Huang` shows how many stories with that word in an English headline sit in a thread. It lists headlines only.

## Trust checks

Nothing is spoken that cannot be traced, and nothing is spoken that recommends a trade.

- **Extraction.** Live import sends one article per request to the small model and asks for figures with the verbatim sentence they come from and a short name for what each measures. A figure is kept only if its evidence is found in the article, the value and unit are printed in that evidence, and the name is built from the article's own words.
- **Narration.** A briefing is assembled from those facts as "Name, amount." sentences. No model writes or rearranges a listener's briefing, so a figure cannot be attached to a different claim. Sources that give different values for one measure are both read and labelled as conflicting.
- **No investment advice.** Stock picks, fund and ETF recommendations, an investment publisher's round-ups of its own picks, "should you buy" pieces, upgrades and downgrades, trading signals and price predictions are recognised by their headline and never selected. Inside a news report, a rating, a rank, a price target or a chart level (support, resistance) is never read out. The gate is deliberately wide: withholding a news story costs one story, reading out a recommendation costs trust. Every briefing opens by saying that the voice is synthetic and that it is not investment advice.
- **`audit-claims PATH`** re-reads the original feeds and checks every stored fact (evidence verbatim in its article, figure printed in the evidence, name in the article's words, source linked), every translated label, and every prepared narration (each spoken number is the value of a cited fact). It makes no provider call.

```powershell
$env:BRIEFING_MODE = "live"; $env:BRIEFING_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app audit-claims "C:/path/event-data.zip"
```

On the full event corpus (1,740 articles, 1,361 stories): 10,163 of 10,163 figures are verbatim in their article with the figure printed in the evidence; 6,382 of 6,382 measure names use the article's words; 1,261 translated headlines and 7,730 translated measure names pass their checks; 17,802 of 17,802 prepared narrations speak only cited figures; 635 stories are withheld as investment advice (the corpus is heavy with one investment publisher's stock-pick content).

## Shared recommendations and summaries

The listener's topic weights form an interest vector. The app ranks eligible stories against that vector, applies topic exclusions, and uses the same weights to choose a prepared angle for each selected story. Language and expertise select a version of that angle. A story about any one of a listener's interests ranks ahead of unrelated headline news, however many interests they named; recency, coverage by several outlets and belonging to a developing event order the rest. A listener's next briefing starts with stories they have not heard.

Each import prepares a neutral summary and up to three evidence-backed angles per eligible story. Summary text is prepared locally, with the same fact IDs, sources, and conflict labels across angles. Briefing requests select and assemble the prepared segments. Older databases can still serve briefings. Reimport the corpus to store the shared summaries so workers can load them directly after a restart.

Audio clips are synthesized on first use and reused across listeners. The app assembles clips in briefing order and reuses the complete audio file when another briefing has the same ordered clips. This saves synthesis work across requests. Disk usage still grows with the number of distinct audio sequences, and reuse across workers requires the same audio directory on a filesystem that supports file locking.

## Languages

Briefings can be listened to in English, German (Deutsch) and Dutch (Nederlands) in demo and live mode. The "Output language" menu lists them by their own names; the page itself stays in English. The choice applies to saved profiles, custom briefings and schedules alike. Set `BRIEFING_MULTILINGUAL=0` to offer English only; the setting is read on every request.

- **Narration.** Labels, units, currency words and the introduction are written in the listener's language, with decimal commas ("3,25 Prozent"). Figures come from the verified facts of whichever edition states them, so every sentence still cites its sources. A figure whose scale or unit cannot be read faithfully is left out instead of guessed. Examples are German "Billionen" (a trillion), "Tausend", "Prozentpunkte" and Dutch "biljoen", "duizend", "keer".
- **Headlines.** A German or Dutch briefing shows each story under the title of its edition in that language and the voice reads it before the figures. Where a story has no edition in that language, a live corpus uses the checked translation of the headline (see *Labels in the listeners' other languages*) and marks it as translated. Without either, the original title stays as the heading and the voice reads no headline.
- **Measure names.** A name the extraction model gave a figure is spoken in its article's language, and in another language through its checked translation. On the event corpus this took German and Dutch from 620 of 1,049 stories read as unnamed figures to 42 of 657.
- **Voices.** Live speech uses `eleven_multilingual_v2`, which speaks all three languages. Demo mode uses eSpeak NG's `de` and `nl` voices, or on Windows an installed System.Speech voice for that language. Without one it falls back to the English voice, so audio is always produced.
- **Limits.** Translated labels are machine translations of short phrases; they are checked for numbers, not for style. The fixture (demo mode) has no translations, so there only stories with a German or Dutch edition get a localized headline. The listening quality of the offline voices is basic. Judge the final sound with ElevenLabs.

## Live providers and costs

Install `backend/requirements-live.txt` and make FFmpeg available on `PATH` to assemble multi-story MP3 briefings. Set `BRIEFING_MODE=live`, `ALLOW_EXTERNAL_PROVIDER_DATA=1`, `OPENAI_API_KEY`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID` in the backend environment. Import before launching the server. Only use live event processing if the organizers permit those provider transfers, and pass `--allow-provider-processing` to the event import in that case. German and Dutch are on by default; check a German and a Dutch briefing by ear before the demo (see Languages).

The application records token usage for OpenAI and submitted text characters for ElevenLabs. Set the rates for your selected models and account to calculate USD estimates:

| Variable | Unit |
| --- | --- |
| `OPENAI_EMBEDDING_INPUT_USD_PER_MILLION_TOKENS` | USD per million embedding input tokens |
| `OPENAI_INPUT_USD_PER_MILLION_TOKENS` | USD per million text input tokens |
| `OPENAI_OUTPUT_USD_PER_MILLION_TOKENS` | USD per million text output tokens |
| `OPENAI_SCRIPT_INPUT_USD_PER_MILLION_TOKENS` | Optional input rate for briefing narration |
| `OPENAI_SCRIPT_OUTPUT_USD_PER_MILLION_TOKENS` | Optional output rate for briefing narration |
| `ELEVENLABS_USD_PER_1000_CHARS` | USD per 1,000 submitted characters |

Costs are split into two figures:
- `preparation_usd` covers ingestion, story extraction and label translation for the corpus currently loaded. A successful reimport replaces the preparation totals. If a reimport fails, the report continues to describe the previously loaded corpus.
- `per_briefing_usd` is script spend averaged over saved briefings, plus speech spend averaged over briefings that received audio, including cache hits and successful retries. Speech-stage calls count clips synthesized by the provider. If audio has yet to succeed for any briefing, incurred speech spend is spread over saved briefings.

USD figures are calculated from the recorded tokens and characters with the rates set when the report is built, so you can set or correct rates after a run. If a rate is missing for a stage that was used, the matching figure is unavailable. The 10x preparation projection assumes the same article mix, model routing, and unit rates. It does not multiply the cost of one listener's briefing.
Briefing narration is deterministic: it is assembled from verified "Label, amount." sentences with no model call per listener, so listeners who should hear the same story get identical text and share one cached audio clip, at any hour. Audio is rendered on first request and reused by everyone after, so the cost per listener falls as the audience grows; only a listener's short refresher line is personal. `OPENAI_BRIEFING_MODEL` (default `gpt-4.1`) is used only if code calls the optional live arrangement step, which briefings no longer do. Set `ELEVENLABS_MODEL` to choose the speech model (defaults to `eleven_multilingual_v2`).

In live mode, listeners choose a voice for their next briefing: the server's default voice (`ELEVENLABS_VOICE_ID`) or one of five ElevenLabs news-reader voices: Daniel and Brian (male), Lily, Matilda and Sarah (female). Every voice bills 1 credit per character on `eleven_multilingual_v2`, the same as the default, and can speak all of that model's languages; ElevenLabs has verified Brian and Lily in German and Dutch. **Play sample** plays ElevenLabs' own preview of the voice; the server fetches it once, caches it in the audio folder, and uses no character credits. Each voice renders and caches its own clips, so a briefing in a new voice costs credits once. Demo mode uses the local system voice and hides the menu. To replace the built-in list, set `BRIEFING_VOICE_PRESETS` to a JSON object such as:

```json
{"default":{"label":"Default voice","voice_id_env":"ELEVENLABS_VOICE_ID"},"calm":{"label":"Calm","voice_id_env":"ELEVENLABS_CALM_VOICE_ID"}}
```

Set the actual voice IDs in the named server environment variables. An unavailable preset uses the existing default voice.

Use a fresh event data directory for a clean measurement because the cost view accumulates calls, including retries. The 10x preparation projection assumes the same article mix, model routing, and unit rates. It does not multiply the cost of one listener's briefing. Missing rates leave the USD estimate unavailable.

### Scalability and cost

Prices are the vendors' official list prices: gpt-4o-mini $0.15/$0.60, text-embedding-3-small $0.02 per million tokens; ElevenLabs Multilingual v2 $0.08 per 1,000 characters on every plan.

**News preparation is shared by all listeners.** Measured live import of the full dataset (1,740 articles), from `costs`:

| Step | Calls | Tokens (in / out) | Cost |
|---|---|---|---|
| Embeddings, to merge reports across outlets | 6 | 395,696 | $0.008 |
| Extraction, one article per request (gpt-4o-mini) | 1,740 | 2,590,920 / 810,107 | $0.875 |
| Headlines and measure names for German and Dutch listeners (gpt-4o-mini) | 961 | 230,572 / 91,800 | $0.090 |
| **Full dataset** | | | **$0.97** |
| **Ten times the volume** | | | **$9.72** |

That is about $0.00056 per article. A weekday of 172 to 190 articles costs about $0.10 to prepare, whatever the number of listeners. Grouping, validation, naming, ranking and narration are code and cost nothing; no large model is used, and no model is called when a listener asks for a briefing. OpenAI's Batch API would halve preparation (about $0.05 a day at this volume) at up to 24 hours' delay. It is not implemented: it suits a nightly production run, and the saving is too small to trade for that delay here.

**Audio is paid once per story version, then shared.** A story clip is rendered the first time a listener needs it in a given language, expertise level and voice, then every later listener with the same version reuses it, at any hour. Only the short refresher line is personal. Speed is changed by the player on the existing file, so it is free.

`backtest-audio` simulates an audience on the imported corpus without calling a provider: each listener gets a profile or a wildcard, a language and a voice, and asks for a briefing on consecutive days with their memory of heard facts. It counts the clips requested and the clips that would be rendered, and checks every briefing. On the event corpus (3 days, 6 voices, 3 languages), audio cost per listener per month of daily briefings:

| Daily listeners | Typical choices | Choices spread evenly (worst case) | Clips reused | If nothing were shared |
|---|---|---|---|---|
| 100 | $1.93 | $2.52 | 55% | $4.09 |
| 1,000 | $0.59 | $0.76 | 86% | $3.97 |
| 10,000 | $0.11 | $0.12 | 97.5% | $3.92 |

"Typical" is 70% English, half on the default voice, 70% on a saved profile. In the same run, 0 of 688,044 spoken facts were replayed to a listener who had already heard them, and 0 were spoken without their source. The month is extrapolated from the 3 simulated days on a fixed corpus, in which later days cost less than the first; pricing every day at the first day's rate gives $0.14 at 10,000 listeners.

```powershell
$env:BRIEFING_MODE = "live"; $env:BRIEFING_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app backtest-audio --listeners 100,1000,10000 --days 3 --both
```

**Decision: no expressiveness setting.** Listeners choose among six voices, which is the customisation they notice. A voice-style option (steady, default, expressive) was built and removed: every level renders every story again in every voice and language, multiplying the versions to render threefold for a difference most listeners do not hear. The table below is the model that decision was made on, before the backtest existed (it is a calculation, not a measurement): worst-case audio cost per listener per month, with choices spread evenly across the options, 4 stories per daily briefing, about 200 characters per story clip and one refresher line in three briefings:

| Daily listeners | Voices only (shipped) | Voices and three styles (removed) |
|---|---|---|
| 100 | $1.80 ($180 a month) | $1.87 |
| 1,000 | $1.15 ($1,149 a month) | $1.62 |
| 10,000 | $0.30 ($3,025 a month) | $0.76 |
| 100,000 | $0.07 ($6,915 a month) | $0.12 |

The cost per listener falls as the audience grows, because each story version is rendered once for everyone who needs it.

## Scheduled delivery

The schedule form is hidden by default because the n8n workflows are inactive and no production service is configured. To show it for development, build the frontend with `VITE_SHOW_SCHEDULE=1` (for example `$env:VITE_SHOW_SCHEDULE = "1"; npm run build`). The API and the local demo timer are unaffected.

Listener settings save one daily or weekly delivery time in the browser's selected IANA timezone. Each browser stores its own listener ID. Scheduled briefings appear at the delivery time with their cited sources. Story ratings and recent spoken facts are stored separately for each listener. The output language and article region are independent. To enable region filters for an imported corpus, supply explicit `region` or `country` metadata and reimport existing records as needed.

Demo mode uses a local timer when n8n is unconfigured. Keep the backend running during generation and delivery. The first eligible occurrence is one with a full 30-minute generation window. During daylight saving changes, a missing local time shifts to the first valid minute, and a repeated local time uses its first occurrence.

The prototype ranker supports scheduled relevance, explicit regions, and seven-day fact suppression in this checkout. Apply the separate recommendation patch to enable the full feedback-aware ranking interface. See the [n8n Cloud setup](automation/SETUP.md) for the workflow exports, credential setup, and deployment checks. Activation requires a public HTTPS backend and durable storage for both SQLite and audio files.

## Demo and cleanup

Generate two distinct profiles, audit three spoken claims against their source articles in the Traces view, generate a wildcard live, and open the evidence and cost views. For the presentation, show the full corpus measurement and 10x estimate. Pitch a newsroom publisher as the buyer and show how editorial review fits between batch preparation and audio delivery. [docs/judging-evidence.md](docs/judging-evidence.md) lists the measured numbers against each judging criterion.

Play each profile's briefing once before presenting: its clips are then cached, so it opens at once and costs nothing more. A wildcard in a new language or voice takes about fifteen seconds the first time, while its clips are rendered.

Run the fixture checks with `./.venv/Scripts/python.exe -m unittest discover -s backend/tests`. The fictional fixture's expected clusters and values live in `backend/demo/answer-key.json` and are checked by the test suite.

After the event, stop the backend and purge the event data:

```powershell
$env:BRIEFING_CORPUS = "event"
./.venv/Scripts/python.exe -m backend.app purge-event --confirm
```

This removes the app databases, audio, and the folders the app created, for both event modes, under the current `BRIEFING_DATA_DIR`. It is safe to repeat. If you used other data directories, set `$env:BRIEFING_DATA_DIR` to each one and run it again. If the backend is still running, the command stops with "Stop the backend" instead of half-deleting. Also delete the original ZIP, the `BRIEFING_PROFILES` file, and any other copies you downloaded, extracted, exported, or backed up. Keep the licensed data and derivatives out of Git and shared storage.

If the backend reports a database error after you update the code, run `reset-demo --confirm` (fixture) or `purge-event --confirm` (event) and import again.
