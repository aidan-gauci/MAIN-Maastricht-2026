import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, join } from 'node:path';
import { createInterface } from 'node:readline';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const repoRoot = fileURLToPath(new URL('../../', import.meta.url));

async function startServer(t, dataDirectory) {
  const server = spawn(process.env.PYTHON || 'python', ['frontend/tests/fixture_server.py'], {
    cwd: repoRoot,
    env: {
      ...process.env,
      PYTHONPATH: [repoRoot, process.env.PYTHONPATH].filter(Boolean).join(process.platform === 'win32' ? ';' : ':'),
      BRIEFING_DATA_DIR: dataDirectory,
      BRIEFING_CORPUS: 'fixture',
      BRIEFING_MODE: 'demo',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const exited = new Promise(resolve => server.once('close', resolve));
  let stderr = '';
  let spawnError;
  server.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
  server.on('error', error => { spawnError = error; });
  t.after(async () => {
    server.kill();
    await exited;
    await rm(dataDirectory, { recursive: true, force: true });
  });

  const lines = createInterface({ input: server.stdout });
  let origin;
  lines.on('line', line => {
    if (line.startsWith('SMOKE_PORT=')) origin = `http://127.0.0.1:${line.split('=')[1]}`;
  });
  // Up to a minute: on a busy machine the backend alone can take over ten seconds to import.
  for (let attempt = 0; attempt < 600; attempt++) {
    if (spawnError || server.exitCode !== null) break;
    if (origin) {
      const response = await fetch(`${origin}/api/profiles`).catch(() => null);
      if (response?.ok) return origin;
    }
    await delay(100);
  }
  throw new Error(`Fixture app failed to start. ${spawnError ?? stderr}`);
}

function postResponse(page, path) {
  return page.waitForResponse(response =>
    new URL(response.url()).pathname === path && response.request().method() === 'POST',
  );
}

test('built listener generates briefings, traces evidence, shows costs, and recovers missing audio', { timeout: 75_000 }, async t => {
  await readFile(join(repoRoot, 'frontend', 'dist', 'index.html'));
  const dataDirectory = await mkdtemp(join(tmpdir(), 'briefing-browser-'));
  const origin = await startServer(t, dataDirectory);
  const browser = await chromium.launch({ channel: 'chromium' });
  t.after(() => browser.close());

  await t.test('shows fallback when JavaScript is disabled', async () => {
    const context = await browser.newContext({ javaScriptEnabled: false });
    try {
      const page = await context.newPage();
      await page.goto(origin);
      await page.getByRole('heading', { name: 'JavaScript is required to use Maze' }).waitFor();
    } finally {
      await context.close();
    }
  });

  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.setDefaultTimeout(8_000);
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(origin);
  await page.getByRole('img', { name: 'Maze' }).waitFor();
  assert.equal(await page.title(), 'Maze | Find your way through the news');
  await page.getByRole('heading', { level: 1, name: 'Find your way through the news.' }).waitFor();

  // Theme: follows the OS by default; the header toggle overrides it, swaps the logo and survives a reload.
  const tokens = () => page.evaluate(() => {
    const names = new Set();
    for (const sheet of document.styleSheets) for (const rule of sheet.cssRules) {
      if (rule.selectorText === ':root') for (const name of rule.style) if (name.startsWith('--')) names.add(name);
    }
    const style = getComputedStyle(document.documentElement);
    return Object.fromEntries([...names].map(name => [name, style.getPropertyValue(name).trim()]));
  });
  const themeState = () => page.evaluate(() => ({
    theme: document.documentElement.dataset.theme,
    scheme: getComputedStyle(document.documentElement).colorScheme,
    logos: [...document.querySelectorAll('.brand-logo')].filter(img => img.getClientRects().length > 0).map(img => img.getAttribute('src')),
  }));
  const themeButton = name => page.getByRole('group', { name: 'Color theme' }).getByRole('button', { name, exact: true });
  await page.emulateMedia({ colorScheme: 'light' });
  const lightTokens = await tokens();
  const systemLight = await themeState();
  assert.deepEqual([systemLight.theme, systemLight.scheme, systemLight.logos.length], ['system', 'light', 1]);
  assert.match(systemLight.logos[0], /maze-logo-for-light-mode/);
  await page.emulateMedia({ colorScheme: 'dark' });
  const systemDark = await themeState();
  assert.equal(systemDark.scheme, 'dark', 'System follows the OS preference');
  assert.equal(systemDark.logos.length, 1);
  assert.match(systemDark.logos[0], /maze-logo-for-dark-mode/);
  const systemDarkTokens = await tokens();
  assert.notEqual(systemDarkTokens['--bg'], lightTokens['--bg']);
  await page.emulateMedia({ colorScheme: 'light' });
  await themeButton('Dark').click();
  assert.equal(await themeButton('Dark').getAttribute('aria-pressed'), 'true');
  const forcedDark = await themeState();
  assert.deepEqual([forcedDark.theme, forcedDark.scheme], ['dark', 'dark']);
  assert.match(forcedDark.logos.join(), /^[^,]*maze-logo-for-dark-mode[^,]*$/, 'dark logo only');
  assert.deepEqual(await tokens(), systemDarkTokens, 'the OS-dark and forced-dark palettes must be identical');
  assert.equal(await page.evaluate(() => localStorage.getItem('maze-theme')), 'dark');
  await page.reload();
  await page.getByRole('img', { name: 'Maze' }).waitFor();
  assert.equal((await themeState()).theme, 'dark', 'saved choice is applied after a reload');
  await page.emulateMedia({ colorScheme: 'dark' });
  await themeButton('Light').click();
  const forcedLight = await themeState();
  assert.deepEqual([forcedLight.theme, forcedLight.scheme], ['light', 'light']);
  assert.match(forcedLight.logos.join(), /^[^,]*maze-logo-for-light-mode[^,]*$/, 'light logo only');
  assert.deepEqual(await tokens(), lightTokens);
  await themeButton('System').click();
  await page.emulateMedia({ colorScheme: 'light' });
  assert.equal(await page.evaluate(() => localStorage.getItem('maze-theme')), 'system');
  assert.equal((await themeState()).theme, 'system');

  const create = page.getByRole('button', { name: 'Create my briefing' });
  await page.getByLabel('Choose a listener profile').selectOption('markets');
  const [profileResponse] = await Promise.all([postResponse(page, '/api/briefings'), create.click()]);
  assert.equal(profileResponse.status(), 201);
  const briefing = await profileResponse.json();
  assert.equal(briefing.profile_name, 'Business and Markets Professional');
  assert.equal(briefing.audio_status, 'ready');
  assert.ok(briefing.segments.length > 1, 'fixture must render assembled audio');

  const narration = page.locator('audio');
  await page.waitForFunction(() => document.querySelector('audio')?.readyState >= HTMLMediaElement.HAVE_METADATA);
  await narration.evaluate(player => player.play());
  await page.getByText('Now playing').waitFor();
  await narration.evaluate(player => player.pause());

  const story = page.locator('.story').first();
  const transcript = story.locator('.transcript-segment > p').first();
  const transcriptText = await transcript.innerText();
  const firstStory = briefing.stories[0];
  const evidenceData = await (await page.request.get(`${origin}/api/evidence`)).json();
  const firstEvidence = evidenceData.find(item => item.id === firstStory.id);
  assert.ok(firstEvidence, 'first briefing story has evidence');
  const firstSegment = briefing.segments.find(segment =>
    firstEvidence.facts.some(fact => segment.fact_ids.includes(fact.id)),
  );
  assert.ok(firstSegment, 'first story has a transcript segment with cited facts');
  await story.getByRole('button', { name: /^\d+ sources?$/ }).first().click();
  const source = story.getByRole('link').first();
  assert.equal(await source.getAttribute('href'), firstSegment.sources[0].url);
  assert.equal(await source.getAttribute('target'), '_blank');
  const inspect = story.getByRole('button', { name: /^Inspect evidence for / });
  await inspect.click();
  const evidence = page.getByRole('heading', { name: 'Evidence for this story' });
  await evidence.waitFor();
  assert.equal(await evidence.evaluate(heading => heading === document.activeElement), true);
  await page.getByRole('heading', { name: firstStory.title, exact: true }).waitFor();
  await page.getByText('Extracted claims', { exact: true }).waitFor();
  assert.ok(await page.locator('.fact blockquote').count() > 0);
  assert.ok((await page.locator('.evidence-sources a').evaluateAll(links =>
    links.map(link => link.getAttribute('href')),
  )).includes(firstSegment.sources[0].url));
  await page.getByRole('button', { name: 'Back to briefing story' }).click();
  assert.equal(await inspect.evaluate(button => button === document.activeElement), true);

  // Linked traces: spoken claim -> extracted facts -> source articles, reached from the transcript.
  const trace = story.getByRole('button', { name: /^Trace the claims in this segment/ }).first();
  await trace.click();
  await page.getByRole('heading', { name: 'Every claim, linked to its source.' }).waitFor();
  const tracePanel = page.locator(`#trace-panel-${firstSegment.id}`);
  const traceToggle = page.locator(`[aria-controls="trace-panel-${firstSegment.id}"]`);
  assert.equal(await traceToggle.getAttribute('aria-expanded'), 'true');
  assert.equal(await traceToggle.evaluate(button => button === document.activeElement), true);
  await tracePanel.locator('.trace-spoken .trace-quote').first().waitFor();
  assert.ok(await tracePanel.locator('.trace-fact').count() > 0, 'trace shows facts behind the spoken claim');
  const traceFactIds = await tracePanel.locator('.trace-fact code').allInnerTexts();
  for (const factId of firstSegment.fact_ids) assert.ok(traceFactIds.includes(factId), `trace lists ${factId}`);
  const traceLinks = await tracePanel.locator('.trace-source a').evaluateAll(links => links.map(link => link.getAttribute('href')));
  assert.ok(traceLinks.includes(firstSegment.sources[0].url), 'trace links to the cited article');
  assert.equal(await tracePanel.locator('.trace-source a').first().getAttribute('target'), '_blank');
  if (new Set(firstSegment.sources.map(item => item.article_id)).size > 1) {
    await tracePanel.getByText(/^\d+ (articles|editions) agree$/).first().waitFor();
  }
  await page.getByRole('button', { name: 'Expand all segments' }).click();
  if (briefing.segments.some(item => item.conflict)) {
    await page.locator('.trace-conflict').first().waitFor();
    await page.locator('.trace-conflict .trace-chip.warn').first().waitFor();
  }
  await page.getByText('Every spoken figure traces to cited reporting.').waitFor();
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'traces fit the phone viewport');
  await page.getByRole('button', { name: 'Back to briefing' }).click();
  assert.equal(await trace.evaluate(button => button === document.activeElement), true);
  await trace.click();
  assert.equal(await traceToggle.evaluate(button => button === document.activeElement), true, 'a second jump focuses the segment again');
  await page.getByRole('button', { name: 'Back to briefing' }).click();

  await page.getByRole('button', { name: 'Costs', exact: true }).click();
  const costs = page.getByRole('table');
  await costs.getByRole('cell', { name: 'speech_generation' }).waitFor();
  const totalRow = costs.getByRole('row').filter({ hasText: 'Per briefing total' });
  await totalRow.getByRole('cell', { name: '$0.0000', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Listen', exact: true }).click();

  const assemblyFailure = join(dataDirectory, 'fail-assembly');
  await rm(join(dataDirectory, 'fixture', 'demo', 'audio', basename(briefing.audio_url.split('?')[0])));
  await writeFile(assemblyFailure, '');
  // Seek via a fresh media request so Chromium cannot serve buffered playback.
  await narration.evaluate(player => {
    player.src = `${player.src}?smoke=deleted`;
    player.load();
  });
  const reload = page.getByRole('button', { name: 'Reload audio' });
  await reload.waitFor();
  const audioPath = `/api/briefings/${briefing.id}/audio`;
  const [failedResponse] = await Promise.all([postResponse(page, audioPath), reload.click()]);
  const failed = await failedResponse.json();
  assert.equal(failed.audio_status, 'unavailable');
  assert.deepEqual(failed.segments, briefing.segments);
  assert.equal(await transcript.innerText(), transcriptText);
  await page.getByRole('alert').filter({ hasText: 'Audio is still unavailable' }).waitFor();

  await rm(assemblyFailure);
  const retry = page.getByRole('button', { name: 'Retry audio', exact: true });
  const [recoveredResponse] = await Promise.all([postResponse(page, audioPath), retry.click()]);
  const recovered = await recoveredResponse.json();
  assert.equal(recovered.audio_url, briefing.audio_url);
  assert.equal(recovered.audio_status, 'ready');
  await page.waitForFunction(() => document.querySelector('audio')?.readyState >= HTMLMediaElement.HAVE_METADATA);
  assert.match(await narration.getAttribute('src'), /[?&]retry=\d+$/);
  assert.equal(await transcript.innerText(), transcriptText);

  await page.getByRole('button', { name: 'Custom briefing' }).click();
  assert.equal(await create.isDisabled(), true);
  const topic = page.getByRole('group', { name: 'Topics to follow' }).getByRole('button').first();
  await topic.click();
  await page.getByLabel('Depth').selectOption('intermediate');
  await page.getByLabel('Length').selectOption('5');
  const [customResponse] = await Promise.all([postResponse(page, '/api/briefings'), create.click()]);
  assert.equal(customResponse.status(), 201);
  // The chip is shown capitalised by CSS; the request carries the topic as the server names it (the DOM text).
  assert.deepEqual(customResponse.request().postDataJSON().wildcard.topics, [(await topic.textContent()).trim()]);
  assert.equal(await topic.evaluate(element => getComputedStyle(element).textTransform), 'capitalize');
  const custom = await customResponse.json();
  assert.notEqual(custom.id, briefing.id);
  assert.equal(custom.audio_status, 'ready');
  const customMeta = page.locator('.briefing-meta .briefing-for');
  await customMeta.getByText(custom.profile_name, { exact: true }).waitFor();
  await customMeta.getByText('English', { exact: true }).waitFor();

  // German and Dutch are offered by name in their own language, and the briefing follows the choice.
  const outputLanguage = page.getByLabel('Output language');
  assert.deepEqual(await outputLanguage.locator('option').allInnerTexts(), ['English', 'Deutsch', 'Nederlands']);
  await outputLanguage.selectOption('de');
  const [germanResponse] = await Promise.all([postResponse(page, '/api/briefings'), create.click()]);
  assert.equal(germanResponse.status(), 201);
  const germanRequest = germanResponse.request().postDataJSON();
  assert.deepEqual([germanRequest.language, germanRequest.wildcard.language], ['de', 'de']);
  const german = await germanResponse.json();
  assert.equal(german.language, 'de');
  assert.equal(german.audio_status, 'ready');
  assert.match(german.segments[0].text, /^(?:Einfach gesagt steht Folgendes in den Quellen\.|Das berichten die Quellen\.|Das sind die berichteten Kennzahlen\.)/);
  await customMeta.getByText('Deutsch', { exact: true }).waitFor();

  // Listen view: the disclosure says the voice is AI-generated and that figures are not investment advice.
  await page.locator('.voice-disclosure').getByText(/The narration is an AI-generated voice\..*This briefing reports figures; it is not investment advice\..*Listen critically/).waitFor();

  // Evidence: the counts line, and merge badges that make merged editions visible.
  const evidenceStories = evidenceData.length;
  const evidenceArticles = evidenceData.reduce((sum, item) => sum + item.articles.length, 0);
  const evidenceClaims = evidenceData.reduce((sum, item) => sum + item.facts.length, 0);
  const noOverflow = () => page.evaluate(() => {
    const card = document.querySelector('.content-card');
    return document.documentElement.scrollWidth <= window.innerWidth && card.scrollWidth <= card.clientWidth;
  });
  await page.getByRole('button', { name: 'Evidence', exact: true }).click();
  const counts = page.locator('.evidence-summary span');
  await counts.first().waitFor();
  // The fixture has no event threads, so no events figure: stories, articles and claims.
  assert.deepEqual(await counts.allTextContents(), [`${evidenceStories} stories`, `${evidenceArticles} articles`, `${evidenceClaims} extracted claims`]);
  await page.getByText(`${evidenceArticles} articles merged into ${evidenceStories} stories.`).waitFor();
  assert.equal(await page.locator('.event-toggle').count(), 0, 'no event headers without threads');
  const storyCard = title => page.locator('article.evidence-card').filter({ has: page.getByRole('button', { name: new RegExp(title) }) });
  const centralBank = storyCard('Central bank holds rate');
  const editionsBadge = centralBank.locator('.merge-badge', { hasText: /^3 editions/ });
  await editionsBadge.waitFor();
  assert.match(await editionsBadge.innerText(), /EN\s*·\s*DE\s*·\s*NL/);
  const translated = evidenceData.filter(item => new Set(item.articles.map(article => article.language)).size > 1).length;
  assert.equal(await page.locator('.merge-badge', { hasText: /editions/ }).count(), translated, 'only translated stories carry the editions badge');
  assert.equal(await page.locator('.merge-badge', { hasText: /outlets/ }).count(), 0, 'no outlets badge when every story has one outlet');
  await centralBank.getByRole('button').click();
  await centralBank.getByText('Merged from 3 articles:').waitFor();
  assert.equal(await centralBank.locator('.evidence-sources a .lang-tag').count(), 3);
  assert.ok(await noOverflow(), 'evidence fits the phone viewport');

  // Event threads: the fixture has none, so group its stories in the browser (the first three, and all the rest) and check the grouping.
  const eventA = evidenceData.slice(0, 3);
  const eventB = evidenceData.slice(3);
  await page.route('**/api/evidence', async route => {
    const response = await route.fetch();
    const stories = await response.json();
    for (const story of stories) {
      if (eventA.some(item => item.id === story.id)) Object.assign(story, { thread_id: 'thread-smoke-a', thread_title: 'Smoke test event A' });
      else Object.assign(story, { thread_id: 'thread-smoke-b', thread_title: 'Smoke test event B' });
    }
    await route.fulfill({ response, json: stories });
  });
  await page.reload();
  await page.getByRole('img', { name: 'Maze' }).waitFor();
  await page.getByRole('button', { name: 'Evidence', exact: true }).click();
  await counts.first().waitFor();
  assert.deepEqual(await counts.allTextContents(), [`${evidenceStories} stories`, '2 events', `${evidenceArticles} articles`, `${evidenceClaims} extracted claims`]);
  await page.getByText(`${evidenceStories} stories grouped into 2 events.`).waitFor();
  const kicker = items => {
    const days = (Math.max(...items.map(item => Date.parse(item.publication_date))) - Math.min(...items.map(item => Date.parse(item.publication_date)))) / 86_400_000;
    const outlets = new Set(items.flatMap(item => item.articles.map(article => article.source))).size;
    return `Event · ${items.length} reports ${days === 0 ? 'on a single day' : `over ${days} ${days === 1 ? 'day' : 'days'}`} · ${outlets} ${outlets === 1 ? 'outlet' : 'outlets'}`;
  };
  const toggleA = page.locator('.event-toggle', { hasText: 'Smoke test event A' });
  const toggleB = page.locator('.event-toggle', { hasText: 'Smoke test event B' });
  const isExpanded = (title, expected) => page.waitForFunction(([name, value]) =>
    [...document.querySelectorAll('.event-toggle')].find(button => button.textContent.includes(name))?.getAttribute('aria-expanded') === value, [title, expected]);
  assert.equal(await page.locator('.event-toggle').count(), 2);
  assert.equal((await toggleA.locator('.event-kicker').innerText()).trim(), kicker(eventA));
  assert.equal((await toggleB.locator('.event-kicker').innerText()).trim(), kicker(eventB));
  assert.equal(await toggleA.getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator('.event-members').count(), 0);
  // The bigger event is listed first.
  assert.equal(await page.locator('.event-toggle').first().innerText().then(text => text.includes('Smoke test event B')), true);

  // Searching keeps an event if any member matches and shows only the matching members.
  const search = page.getByLabel('Find a story or publisher');
  await search.fill(eventA[1].title);
  await toggleA.locator('.event-match', { hasText: '1 of 3 match' }).waitFor();
  await isExpanded('Smoke test event A', 'true');
  assert.equal(await page.locator('.event-toggle').count(), 1, 'events without a match are hidden');
  assert.equal(await page.locator('.event-members > article').count(), 1);
  await search.fill('');
  await isExpanded('Smoke test event A', 'false');
  await search.fill('zzz no such story');
  await page.getByText('No stories match your search.').waitFor();
  assert.equal(await page.locator('.event-toggle').count(), 0);
  await search.fill('');

  // The header is a real button: Enter opens it, Space closes it.
  await toggleA.focus();
  await page.keyboard.press('Enter');
  await isExpanded('Smoke test event A', 'true');
  const panelId = await toggleA.getAttribute('aria-controls');
  assert.equal(await page.locator(`#${panelId} > article`).count(), 3);
  assert.match(await page.locator(`#${panelId} .evidence-count`).first().innerText(), /^\d+ \w+ · \d+ sources?$/, 'members show their date');
  assert.ok(await noOverflow(), 'an expanded event fits the phone viewport');
  await page.keyboard.press('Space');
  await isExpanded('Smoke test event A', 'false');
  assert.equal(await page.locator('.event-members').count(), 0);

  // "Inspect evidence" from the Listen view lands on the story; "Browse all evidence" then returns to it inside its event.
  // A listener hears unheard stories first, so this one has used up the ten fixture stories. Continue as a new listener.
  await page.evaluate(() => localStorage.setItem('briefing-listener-id', crypto.randomUUID()));
  await page.reload();
  await page.getByRole('button', { name: 'Listen', exact: true }).click();
  const [againResponse] = await Promise.all([postResponse(page, '/api/briefings'), create.click()]);
  const listened = (await againResponse.json()).stories[0];
  const listenedEvidence = evidenceData.find(item => item.id === listened.id);
  assert.ok(listenedEvidence, 'the first story of the briefing has evidence');
  const listenedEvent = eventA.includes(listenedEvidence) ? 'Smoke test event A' : 'Smoke test event B';
  await page.getByRole('button', { name: `Inspect evidence for ${listened.title}`, exact: true }).click();
  await page.getByRole('heading', { name: 'Evidence for this story' }).waitFor();
  await page.getByRole('heading', { name: listened.title, exact: true }).waitFor();
  assert.equal(await page.locator('.event-toggle').count(), 0, 'one story is shown without its event');
  await page.locator('.event-context').getByText(/^Part of an event · \d+ reports/).waitFor();
  await page.getByRole('button', { name: 'Browse all evidence' }).click();
  await isExpanded(listenedEvent, 'true');
  const storyButton = page.locator(`#evidence-story-${listened.id}`);
  assert.equal(await storyButton.getAttribute('aria-expanded'), 'true');
  await page.waitForFunction(id => document.activeElement?.id === id, `evidence-story-${listened.id}`);
  assert.deepEqual(pageErrors, []);
});
