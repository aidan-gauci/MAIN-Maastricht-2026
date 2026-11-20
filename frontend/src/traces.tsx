import { useEffect, useMemo, useRef, useState } from 'react';
import { ChevronDown, CircleCheck, Link2, LoaderCircle, RotateCw, ShieldCheck, TriangleAlert } from 'lucide-react';

/*
 * Linked Traces: spoken claim -> extracted facts -> source articles.
 *
 * Everything here is derived from data the API already returns:
 *   segment.fact_ids        which facts the narration used
 *   fact.article_id         which article each fact was extracted from
 *   fact.conflict_*         conflicts the pipeline detected
 * The only inference is grouping facts that carry the same numeric value into one
 * "claim" (so equal figures reported in English, German and Dutch show up as one
 * corroborated claim), and matching that value to the spoken sentence that quotes it.
 * The types are structural subsets of the ones in App.tsx.
 */

export type TraceSource = { article_id: string; title: string; source: string; url: string; language?: string };
export type TraceFact = { id: string; statement: string; value: string; unit: string; evidence: string; article_id: string; conflict_status: string; language?: string; conflict_group?: string; conflict_label?: string };
export type TraceEvidence = { id: string; title: string; articles: TraceSource[]; facts: TraceFact[]; conflicts?: { id: string; label: string; fact_ids: string[] }[] };
export type TraceSegmentInput = { id: string; story_id?: string; text: string; fact_ids: string[]; sources: TraceSource[]; conflict: boolean };
export type TraceBriefing = { id: string; stories: { id: string; title: string }[]; segments: TraceSegmentInput[] };
export type TraceFocus = { segmentId: string; nonce: number };

type Status = 'corroborated' | 'single' | 'conflict' | 'untraced';
type Link = { fact: TraceFact; article?: TraceSource };
type Claim = {
  id: string; value: string; spoken: string[]; links: Link[]; sources: TraceSource[];
  languages: string[]; status: Status; conflictId?: string; conflictLabel?: string; order: number;
};
type Block = { id: string; claims: Claim[]; conflict?: { label: string; values: string[]; sources: TraceSource[] } };
type SegmentTrace = {
  id: string; index: number; title: string; blocks: Block[]; claims: Claim[]; factCount: number;
  sources: TraceSource[]; languages: string[]; missing: string[]; conflictCount: number; corroboratedCount: number; singleCount: number;
};

const CONFLICT_STATUSES = ['conflict', 'unresolved', 'needs_review'];
const NUMBER = /\d[\d.,]*\d|\d/g;

/** "3,25", "3.25" -> "3.25"; "12,400" and "12.400" -> "12400". Null when there is no number. */
function canonicalNumber(raw: string): string | null {
  const token = raw.match(NUMBER)?.[0];
  if (!token) return null;
  if (/^\d{1,3}([.,]\d{3})+$/.test(token)) return token.replace(/[.,]/g, '');
  const parsed = Number(token.replace(',', '.'));
  return Number.isFinite(parsed) ? String(parsed) : null;
}

const sentencesOf = (text: string) => text.replace(/([.!?])\s+/g, '$1\u0000').split('\u0000').map(part => part.trim()).filter(Boolean);
const unique = <T,>(items: T[], key: (item: T) => string) => [...new Map(items.map(item => [key(item), item])).values()];
/** Extracted evidence is clipped at a fixed length; mark clipped excerpts instead of presenting them as sentences. */
const excerpt = (text: string) => /[.!?…)"”]$/.test(text.trim()) ? text.trim() : `${text.trim()}…`;
const display = (fact: TraceFact) => [fact.value, fact.unit].filter(Boolean).join(' ');
function languageName(code: string) {
  try { return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) ?? code.toUpperCase(); }
  catch { return code.toUpperCase(); }
}
const plural = (count: number, one: string, many = one + 's') => `${count} ${count === 1 ? one : many}`;

function buildTraces(briefing: TraceBriefing, evidence: TraceEvidence[]) {
  const facts = new Map(evidence.flatMap(story => story.facts).map(fact => [fact.id, fact]));
  const articles = new Map<string, TraceSource>();
  for (const article of [...briefing.segments.flatMap(segment => segment.sources), ...evidence.flatMap(story => story.articles)]) {
    articles.set(article.article_id, { ...articles.get(article.article_id), ...article });
  }
  const conflicts = new Map<string, { id: string; label: string }>();
  for (const story of evidence) for (const conflict of story.conflicts ?? []) for (const id of conflict.fact_ids) conflicts.set(id, conflict);

  const segments: SegmentTrace[] = briefing.segments.map((segment, index) => {
    const story = briefing.stories.find(item => segment.story_id === item.id || segment.id === 'segment-' + item.id)
      ?? briefing.stories.find(item => evidence.find(entry => entry.id === item.id)?.facts.some(fact => segment.fact_ids.includes(fact.id)));
    const ids = [...new Set(segment.fact_ids)];
    const resolved = ids.flatMap(id => facts.get(id) ?? []);
    const sentences = sentencesOf(segment.text);
    const sentenceNumbers = sentences.map(sentence => new Set((sentence.match(NUMBER) ?? []).map(canonicalNumber)));

    // One claim per distinct figure. A second fact with the same value from the same article
    // starts a new claim, because two different measures can share a number.
    const grouped = new Map<string, { number: string | null; links: Link[] }>();
    for (const fact of resolved) {
      const number = canonicalNumber(fact.value);
      const base = number ?? `fact:${fact.id}`;
      let key = base;
      for (let copy = 2; grouped.get(key)?.links.some(link => link.fact.article_id === fact.article_id); copy++) key = `${base}#${copy}`;
      const group = grouped.get(key) ?? { number, links: [] };
      group.links.push({ fact, article: articles.get(fact.article_id) });
      grouped.set(key, group);
    }

    const claims: Claim[] = [...grouped.entries()].map(([key, group], position) => {
      const matches = group.number === null ? [] : sentences.flatMap((sentence, at) => sentenceNumbers[at].has(group.number) ? [{ sentence, at }] : []);
      const sources = unique(group.links.flatMap(link => link.article ?? []), article => article.article_id);
      const languages = [...new Set(group.links.map(link => link.article?.language ?? link.fact.language).filter((code): code is string => Boolean(code)))];
      const conflicted = group.links.find(link => CONFLICT_STATUSES.includes(link.fact.conflict_status));
      const known = conflicted && conflicts.get(conflicted.fact.id);
      const status: Status = conflicted ? 'conflict' : sources.length === 0 ? 'untraced' : sources.length > 1 ? 'corroborated' : 'single';
      return {
        id: `${segment.id}:${key}`, value: display(group.links[0].fact), spoken: [...new Set(matches.map(match => match.sentence))],
        links: group.links, sources, languages, status,
        conflictId: conflicted ? conflicted.fact.conflict_group ?? known?.id ?? `conflict:${key}` : undefined,
        conflictLabel: conflicted ? conflicted.fact.conflict_label ?? known?.label ?? 'Conflicting values reported for the same measure' : undefined,
        order: matches.length ? matches[0].at : sentences.length + position,
      };
    }).sort((left, right) => left.order - right.order);

    // Conflicting claims are drawn together in one block, positioned where the first one is spoken.
    const blocks: Block[] = [];
    for (const claim of claims) {
      if (!claim.conflictId) { blocks.push({ id: claim.id, claims: [claim] }); continue; }
      const existing = blocks.find(block => block.conflict && block.id === claim.conflictId);
      if (existing) { existing.claims.push(claim); continue; }
      blocks.push({ id: claim.conflictId, claims: [claim], conflict: { label: claim.conflictLabel ?? '', values: [], sources: [] } });
    }
    for (const block of blocks) if (block.conflict) {
      block.conflict.values = block.claims.map(claim => claim.value);
      block.conflict.sources = unique(block.claims.flatMap(claim => claim.sources), article => article.article_id);
    }

    const segmentSources = unique(claims.flatMap(claim => claim.sources), article => article.article_id);
    return {
      id: segment.id, index, title: story?.title ?? `Segment ${index + 1}`, blocks, claims, factCount: resolved.length,
      sources: segmentSources, languages: [...new Set(claims.flatMap(claim => claim.languages))],
      missing: ids.filter(id => !facts.has(id)),
      conflictCount: blocks.filter(block => block.conflict).length,
      corroboratedCount: claims.filter(claim => claim.status === 'corroborated').length,
      singleCount: claims.filter(claim => claim.status === 'single').length,
    };
  });

  const claims = segments.flatMap(segment => segment.claims);
  const firstConflict = segments.find(segment => segment.conflictCount > 0);
  return {
    segments,
    claimCount: claims.length,
    tracedCount: claims.filter(claim => claim.sources.length > 0).length,
    factCount: new Set(claims.flatMap(claim => claim.links.map(link => link.fact.id))).size,
    sourceCount: new Set(segments.flatMap(segment => segment.sources.map(article => article.article_id))).size,
    languageCount: new Set(segments.flatMap(segment => segment.languages)).size,
    corroboratedCount: claims.filter(claim => claim.status === 'corroborated').length,
    conflictCount: segments.reduce((total, segment) => total + segment.conflictCount, 0),
    missingCount: segments.reduce((total, segment) => total + segment.missing.length, 0),
    defaultOpen: [...new Set([segments[0]?.id, firstConflict?.id].filter((id): id is string => Boolean(id)))],
  };
}

function Language({ code }: { code?: string }) {
  if (!code) return null;
  return <span className="trace-lang">{code.toUpperCase()}<span className="visually-hidden"> ({languageName(code)})</span></span>;
}

function StatusChip({ claim }: { claim: Claim }) {
  if (claim.status === 'corroborated') {
    // One publisher's translated editions are not independent confirmation; say so.
    const publishers = new Set(claim.sources.map(source => source.source)).size;
    const label = publishers > 1 ? `${claim.sources.length} articles agree` : `${claim.sources.length} editions agree`;
    return <span className="trace-chip ok"><CircleCheck size={13} aria-hidden="true"/>{label}</span>;
  }
  if (claim.status === 'conflict') return <span className="trace-chip warn"><TriangleAlert size={13} aria-hidden="true"/>Conflict flagged</span>;
  if (claim.status === 'untraced') return <span className="trace-chip warn"><TriangleAlert size={13} aria-hidden="true"/>Source not found</span>;
  return <span className="trace-chip"><Link2 size={13} aria-hidden="true"/>Single source</span>;
}

function ClaimRow({ claim, block }: { claim: Claim; block: Block }) {
  const others = block.conflict ? block.claims.filter(item => item !== claim).map(item => item.value) : [];
  const note = claim.status === 'corroborated' ? `Same figure in ${claim.languages.map(languageName).join(', ')}`
    : claim.status === 'single' ? 'Only one article reports this figure.'
    : claim.status === 'conflict' && others.length ? `Differs from ${others.join(', ')}` : '';
  return <li className={`trace-claim ${claim.status}`}>
    <div className="trace-node trace-spoken">
      <span className="trace-kicker">Spoken claim</span>
      {claim.spoken.length > 0 ? claim.spoken.map(sentence => <p className="trace-quote" key={sentence}>“{sentence}”</p>)
        : <p className="trace-quote unquoted">Cited figure {claim.value}<span>Not quoted word for word in the narration.</span></p>}
      <StatusChip claim={claim}/>
      {note && <span className="trace-note">{note}</span>}
    </div>
    <ul className="trace-facts" aria-label={`Facts and sources behind ${claim.value}`}>
      {claim.links.map(({ fact, article }) => <li className="trace-fact" key={fact.id}>
        <div className="trace-node trace-factcard">
          <span className="trace-kicker">Extracted fact</span>
          <p>{excerpt(fact.statement)}</p>
          <span className="trace-fact-meta"><b>{display(fact)}</b><code>{fact.id}</code></span>
        </div>
        <div className="trace-node trace-source">
          <span className="trace-kicker">From source</span>
          {article ? <a href={article.url} target="_blank" rel="noreferrer">
            <span className="trace-publisher">{article.source}<Language code={article.language ?? fact.language}/></span>
            <strong>{article.title}</strong>
            <span className="trace-open" aria-hidden="true">Open source ↗</span>
          </a> : <p className="trace-missing">Article {fact.article_id} is not in the evidence set.</p>}
        </div>
      </li>)}
    </ul>
  </li>;
}

function Message({ text, busy = false }: { text: string; busy?: boolean }) {
  return <div className="data-loading" role="status">{busy && <LoaderCircle className="spin" size={18} aria-hidden="true"/>} {text}</div>;
}

export function TracesView({ briefing, evidence, available, availableAt, loading, error, onRetry, focus, onReturn }: {
  briefing: TraceBriefing | null;
  evidence: TraceEvidence[];
  available: boolean;
  availableAt?: string | null;
  loading: boolean;
  error: string;
  onRetry: () => void;
  focus: TraceFocus | null;
  onReturn: () => void;
}) {
  const model = useMemo(() => briefing && available ? buildTraces(briefing, evidence) : null, [briefing, evidence, available]);
  const [openIds, setOpenIds] = useState<string[] | null>(null);
  const headers = useRef<Record<string, HTMLButtonElement | null>>({});
  const handled = useRef(0);
  const open = new Set(openIds ?? model?.defaultOpen ?? []);

  useEffect(() => { setOpenIds(null); }, [briefing?.id]);

  useEffect(() => {
    if (!focus || !model || handled.current === focus.nonce) return;
    const header = headers.current[focus.segmentId];
    if (!header) return;
    handled.current = focus.nonce;
    setOpenIds(current => [...new Set([...(current ?? model.defaultOpen), focus.segmentId])]);
    header.focus();
    header.closest('li')?.scrollIntoView({ block: 'start' });
  }, [focus, model]);

  const toggle = (id: string) => setOpenIds(open.has(id) ? [...open].filter(item => item !== id) : [...open, id]);
  const allOpen = Boolean(model) && model!.segments.every(segment => open.has(segment.id));
  const complete = Boolean(model) && model!.claimCount > 0 && model!.tracedCount === model!.claimCount;

  return <div className="data-view trace-view">
    <div className="data-heading">
      <span className="step-label">LINKED TRACES</span>
      <h2>Every claim, linked to its source.</h2>
      <p>Follow each spoken figure to the facts extracted from the reporting, and on to the articles they came from.</p>
    </div>
    {focus && <div className="evidence-actions"><button className="source-toggle" onClick={onReturn}>Back to briefing</button></div>}
    {!briefing ? <Message text="Create a briefing to trace its claims."/>
      : !available ? <Message text={`This briefing is scheduled. Its source traces appear ${availableAt ? new Date(availableAt).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : 'when it is delivered'}.`}/>
      : loading ? <Message text="Loading evidence…" busy/>
      : error ? <div><p className="action-error" role="alert">{error}</p><button className="retry-audio" onClick={onRetry}><RotateCw size={14}/> Retry loading evidence</button></div>
      : !model || model.segments.length === 0 ? <Message text="This briefing has no spoken segments to trace."/> : <>
        <p className={complete ? 'trace-assurance' : 'trace-assurance partial'}>
          {complete ? <ShieldCheck size={18} aria-hidden="true"/> : <TriangleAlert size={18} aria-hidden="true"/>}
          <span><strong>{complete ? 'Every spoken figure traces to cited reporting.' : `${model.tracedCount} of ${model.claimCount} spoken figures trace to cited reporting.`}</strong>
            {' '}{plural(model.claimCount, 'figure')} lead to {plural(model.factCount, 'fact')} in {plural(model.sourceCount, 'source article')}.
            {model.conflictCount > 0 ? ' Where reports disagree, the conflict is marked rather than resolved.' : ''}</span>
        </p>
        <ul className="trace-summary" aria-label="Trace summary">
          <li><strong>{model.claimCount}</strong><span>spoken figures</span></li>
          <li><strong>{model.factCount}</strong><span>facts used</span></li>
          <li><strong>{model.sourceCount}</strong><span>source articles in {plural(model.languageCount, 'language')}</span></li>
          <li className="ok"><strong>{model.corroboratedCount}</strong><span>matched in 2+ articles</span></li>
          <li className={model.conflictCount ? 'warn' : ''}><strong>{model.conflictCount}</strong><span>{model.conflictCount === 1 ? 'conflict' : 'conflicts'} flagged</span></li>
        </ul>
        <div className="trace-toolbar">
          <ul className="trace-legend" aria-label="Legend">
            <li><span className="trace-chip ok"><CircleCheck size={13} aria-hidden="true"/>Corroborated</span>2+ articles give the same figure</li>
            <li><span className="trace-chip"><Link2 size={13} aria-hidden="true"/>Single source</span>one article</li>
            <li><span className="trace-chip warn"><TriangleAlert size={13} aria-hidden="true"/>Conflict</span>different values for one measure</li>
          </ul>
          <button className="source-toggle" onClick={() => setOpenIds(allOpen ? [] : model.segments.map(segment => segment.id))}>{allOpen ? 'Collapse all segments' : 'Expand all segments'}</button>
        </div>
        <ol className="trace-segments">
          {model.segments.map(segment => {
            const isOpen = open.has(segment.id);
            const panel = `trace-panel-${segment.id}`;
            return <li className="trace-segment" key={segment.id}>
              <h3>
                <button ref={element => { headers.current[segment.id] = element; }} className="trace-segment-toggle" aria-expanded={isOpen} aria-controls={isOpen ? panel : undefined} onClick={() => toggle(segment.id)}>
                  <span className="trace-segment-title">
                    <span className="evidence-count">SPOKEN SEGMENT {segment.index + 1}</span>
                    <strong>{segment.title}</strong>
                    <span className="trace-segment-meta">{plural(segment.claims.length, 'figure')} · {plural(segment.factCount, 'fact')} · {plural(segment.sources.length, 'source')}{segment.languages.length > 1 ? ` in ${segment.languages.map(languageName).join(', ')}` : ''}</span>
                  </span>
                  <span className="trace-segment-chips">
                    {segment.conflictCount > 0 && <span className="trace-chip warn"><TriangleAlert size={13} aria-hidden="true"/>Conflict flagged</span>}
                    {segment.corroboratedCount > 0 && <span className="trace-chip ok"><CircleCheck size={13} aria-hidden="true"/>{segment.corroboratedCount} corroborated</span>}
                    {segment.singleCount > 0 && <span className="trace-chip"><Link2 size={13} aria-hidden="true"/>{segment.singleCount} single-source</span>}
                  </span>
                  <ChevronDown size={17} aria-hidden="true" className={isOpen ? 'rotated' : ''}/>
                </button>
              </h3>
              {isOpen && <div className="trace-panel" id={panel}>
                {segment.blocks.length === 0 && <p className="trace-missing">This segment cites no facts that could be found in the evidence set.</p>}
                {segment.blocks.length > 0 && <div className="trace-cols" aria-hidden="true"><span>Spoken claim</span><span className="trace-cols-pair"><span>Extracted facts</span><span>Source articles</span></span></div>}
                {segment.blocks.map(block => <div className={block.conflict ? 'trace-block trace-conflict' : 'trace-block'} key={block.id}>
                  {block.conflict && <div className="trace-conflict-head">
                    <TriangleAlert size={16} aria-hidden="true"/>
                    <div>
                      <strong>Conflict: {block.conflict.values.join(' vs ')}</strong>
                      <p>{block.conflict.label}. {block.conflict.sources.length === 1
                        ? `All of these figures are cited in the same article (${block.conflict.sources[0].source}), so the narration reports them as conflicting.`
                        : `They come from ${plural(block.conflict.sources.length, 'article')}: ${block.conflict.sources.map(article => article.source).join(', ')}.`}</p>
                    </div>
                  </div>}
                  <ul className="trace-claims">{block.claims.map(claim => <ClaimRow key={claim.id} claim={claim} block={block}/>)}</ul>
                </div>)}
                {segment.missing.length > 0 && <p className="trace-missing">{plural(segment.missing.length, 'cited fact')} could not be found in the evidence set: {segment.missing.join(', ')}.</p>}
              </div>}
            </li>;
          })}
        </ol>
        <p className="trace-footnote">Sources are counted per article, so a publisher's translated editions count separately. Figures are matched by their numeric value within a segment.</p>
        {model.missingCount > 0 && <p className="trace-footnote" role="status">{plural(model.missingCount, 'cited fact')} could not be matched to extracted evidence.</p>}
      </>}
  </div>;
}
