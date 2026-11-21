# Maze: evidence against the judging criteria

Maze turns a publisher's business news into short audio briefings that differ per listener, where every spoken figure can be followed back to the sentence it came from. Everything below was measured on the full event dataset (1,740 articles in English, German and Dutch) with the commands named beside each number. All of them can be re-run; none of the checks calls a provider.

## The numbers to quote

| | |
|---|---|
| Preparing the full dataset (1,740 articles) | **$0.97** |
| The same at ten times the volume | **$9.72** |
| Audio per listener per month, 10,000 daily listeners | **$0.11** |
| Spoken figures that are verbatim in their source article | **10,163 of 10,163** |
| Prepared narrations that speak only cited figures | **17,802 of 17,802** |
| Stories withheld because they are investment advice | **635 of 1,361** |
| Model calls when a listener asks for a briefing | **0** |

## 1. Trust and accuracy (25)

**Claim: nothing is spoken that cannot be traced, and nothing is spoken that recommends a trade.**

- **Every claim links to its source.** In the Traces view each spoken sentence leads to the extracted fact, the verbatim sentence it came from, and the article with its link. To check three claims live: create a briefing, press *Trace claims* on any story, and open the source.
- **Numbers are right because no model touches them after extraction.** The small model proposes a figure together with the sentence it copied it from. The figure is kept only if that sentence is found in the article and the value and unit are printed in it. `audit-claims` re-reads the original feeds: 10,163 of 10,163 stored figures are verbatim in their article, and 6,382 of 6,382 measure names are built from the article's own words.
- **Nothing is invented.** A briefing is assembled from those facts as "Name, amount." sentences by ordinary code. No model writes or rearranges a listener's briefing, so a number cannot be attached to a different claim. All 17,802 prepared narrations (every story in three languages, three depths and up to four angles) speak only figures that are cited.
- **Disagreeing sources are flagged.** When two reports give different values for one measure, both are read, the story says "The reports give conflicting values", and the transcript and Traces view mark the conflict. The dataset has 5 such stories; all 5 are flagged.
- **No investment advice.** The dataset is dominated by one investment publisher's stock-pick content. Maze recognises picks, fund and ETF recommendations, "should you buy" pieces, upgrades and downgrades, trading signals and price predictions by their headline and never selects them: 635 of 1,361 stories are withheld. Inside a news report, a rating, a price target or a chart level is never read out. Every briefing opens with: "This briefing is read by an AI voice. It reports figures from the cited sources and is not investment advice."
- **Translations are checked too.** Headlines and measure names translated for German and Dutch listeners must repeat the numbers of the original and add none: 1,261 headlines and 7,730 names pass. A translated headline is marked as translated, with the original beside it.

## 2. Fit to the listener (20)

**Claim: two listeners get different briefings from the same facts, and a listener you invent on the spot gets theirs in seconds.**

- **Four saved listeners.** Everyday economics (beginner, 3 stories), markets professional (advanced, 5 stories), policy watcher (intermediate, 8 stories) and a German everyday listener (beginner, 3 stories, in German). They differ in which stories are chosen, their order, how many, the opening line, the figures put first, and the language.
- **Wildcard, live.** Pick 1 to 12 of 36 topics, one of three depths, 3, 5 or 8 stories, English, German or Dutch, and one of six voices. A story about any one of the chosen topics ranks ahead of unrelated headline news.
- **It remembers what you heard.** A listener's next briefing starts with stories they have not heard. A figure heard in the last seven days is never read again as news; when an update to the same measure arrives, the earlier value is recalled in one short line ("Last time, the policy rate stood at ...").
- **Feedback.** *Helpful* and *Not useful* on a story move that listener's topic weights for the next briefing.

## 3. Making sense of the news (20)

**Claim: one event is one story, however many articles and languages report it.**

- **Duplicates merged.** 1,604 articles with verified figures become 1,361 stories. All 236 German and Dutch editions joined their English original (118 stories carry three languages). Reports of one event by different outlets are merged by meaning, shared figures and time; daily digests never merge.
- **Developing events.** 208 stories form 56 event threads (for example a leadership change followed over six days). A briefing plays at most one story per thread, and the Evidence view shows each thread with its reports and outlets.
- **Facts are right.** See criterion 1: the grouping never changes a fact, because each fact stays tied to the article it was read from.
- **Most important first.** Stories are ordered by one score that weighs the match to the listener's interests most, then recency, coverage by more than one outlet, and membership of a developing event.
- **Checked offline.** `evaluate-grouping` scores translation matching against the organizers' pairs and reports group sizes and threads.

## 4. Scalability and cost (15)

**Claim: the news is prepared once for everyone, audio is rendered once per story, and no listener triggers a model call.**

Vendor list prices: gpt-4o-mini $0.15 / $0.60 per million tokens, text-embedding-3-small $0.02 per million tokens, ElevenLabs Multilingual v2 $0.08 per 1,000 characters.

**Preparation, measured on the full dataset (`costs`):**

| Step | How | Calls | Cost |
|---|---|---|---|
| Group articles, join translations | rules, no model | 0 | $0 |
| Merge reports across outlets | embeddings | 6 | $0.008 |
| Extract figures with their evidence | small model, one article per request | 1,740 | $0.875 |
| Translate headlines and measure names | small model, one request per story | 961 | $0.090 |
| Validate, name, rank, narrate | code, no model | 0 | $0 |
| **Full dataset** | | | **$0.97** |
| **Ten times the volume** | same mix and prices | | **$9.72** |

That is $0.00056 per article. No large model is used anywhere.

**Audio, simulated on the real corpus (`backtest-audio`, 3 days of daily briefings, 6 voices, 3 languages):**

| Daily listeners | Typical choices | Choices spread evenly (worst case) | Clips reused | If nothing were shared |
|---|---|---|---|---|
| 100 | $1.93 | $2.52 | 55% | $4.09 |
| 1,000 | $0.59 | $0.76 | 86% | $3.97 |
| 10,000 | **$0.11** | $0.12 | 97.5% | $3.92 |

Audio cost per listener per month (30 daily briefings). "Typical" is 70% English, half on the default voice, 70% on a saved profile. In the same run, 0 of 688,044 spoken facts were replayed to a listener who had already heard them and 0 were spoken without their source. The month is extrapolated from 3 simulated days on a fixed corpus, in which later days cost less than the first; pricing every day at the first day's rate gives an upper bound of $0.14 at 10,000 listeners.

A story's clip is rendered the first time anyone needs it in a language, depth and voice, and every later listener reuses it. The cost per listener therefore falls as the audience grows. Playback speed is changed in the player on the existing file, so it is free.

**Decisions made for cost.**
- *No model call per briefing.* Narration is deterministic, so identical stories give identical text and share one clip.
- *No voice-style setting.* It was built and removed: each style level renders every story again in every voice and language for a difference most listeners do not hear.
- *No Batch API yet.* It would halve preparation (about $0.04 a day at this volume) at up to 24 hours' delay; it belongs in a nightly production run, not a demo.

## 5. Listening experience (10)

- **Structure.** Disclosure, one opening line, then for each story its headline and up to four named figures. A name is heard at most twice per story.
- **Written for the ear.** Figures are read as "Name, amount." with units spoken in full ("1.25 percent", "92.12 dollars", "3,5 Prozent"), a short pause after each figure and a longer one between stories.
- **Length.** 3, 5 or 8 stories: about 1, 2 or 3 minutes.
- **Control.** Six voices, three languages, and 0.8x to 1.5x speed in the player.

## 6. Path to product (10)

- **Who pays.** A news publisher or licensor that already owns the reporting. Maze is a way to sell the same articles again as a personal audio product: to subscribers as a retention feature, or to professional clients as a per-seat briefing. At 10,000 daily listeners the audio costs about $0.11 per listener per month (about $1,060 a month in total) and preparation is under a dollar a day.
- **Newsroom workflow.** Overnight batch: import the day's articles (minutes, cents). Morning: an editor opens the Evidence view, sees each story with its figures, sources, conflicts and what was withheld, and can pull a story. Listeners then get briefings on demand; audio is rendered on first request and shared.
- **Synthetic voice disclosure.** Spoken at the start of every briefing, shown on the player ("AI-generated narration") and in *About this audio*.
- **Rights.** Article text is not stored after import. A briefing speaks only headlines and figures; the trace shows the one sentence a figure came from, with a link back to the publisher. One command purges everything.

## Known limits

- Most figures have one source, because most stories in the dataset have one article. The Traces view says so on each claim.
- Narration is a list of named figures by design. It does not explain why a number moved; doing that without inventing would need a second verified layer.
- Translated labels are machine translations of short phrases, checked for numbers but not for style.
- The largest event thread (37 reports about one company) is broader than a single event.
- The advice gate is a set of headline and phrase rules. It is wide on purpose and withholds some ordinary news.
- The prototype runs on one server with SQLite. Scheduled delivery is built but switched off.
