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
      LIQUID_DATA_DIR: dataDirectory,
      LIQUID_CORPUS: 'fixture',
      LIQUID_MODE: 'demo',
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
  for (let attempt = 0; attempt < 100; attempt++) {
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

test('built listener generates briefings, traces evidence, shows costs, and recovers missing audio', { timeout: 45_000 }, async t => {
  await readFile(join(repoRoot, 'frontend', 'dist', 'index.html'));
  const dataDirectory = await mkdtemp(join(tmpdir(), 'liquid-browser-'));
  const origin = await startServer(t, dataDirectory);
  const browser = await chromium.launch({ channel: 'chromium' });
  t.after(() => browser.close());

  await t.test('shows fallback when JavaScript is disabled', async () => {
    const context = await browser.newContext({ javaScriptEnabled: false });
    try {
      const page = await context.newPage();
      await page.goto(origin);
      await page.getByRole('heading', { name: 'JavaScript is required to use Liquid Briefing' }).waitFor();
    } finally {
      await context.close();
    }
  });

  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.setDefaultTimeout(8_000);
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.goto(origin);
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
  await page.getByText('EXTRACTED CLAIMS', { exact: true }).waitFor();
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
    await tracePanel.getByText(/^\d+ sources agree$/).first().waitFor();
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
  assert.deepEqual(customResponse.request().postDataJSON().wildcard.topics, [await topic.innerText()]);
  const custom = await customResponse.json();
  assert.notEqual(custom.id, briefing.id);
  assert.equal(custom.audio_status, 'ready');
  await page.getByText(`${custom.profile_name} · English`, { exact: true }).waitFor();
  assert.deepEqual(pageErrors, []);
});
