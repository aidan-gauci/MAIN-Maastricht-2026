import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { AudioLines, BadgeCheck, BookOpen, Check, ChevronDown, Clock3, Headphones, Info, LoaderCircle, RotateCw, ShieldCheck, Sparkles, Waves } from 'lucide-react';

type Profile = { id: string; name: string; description: string; topic_weights?: Record<string, number>; excluded_topics?: string[]; expertise?: string; language?: string; duration?: number; style?: string };
type Source = { article_id: string; title: string; source: string; url: string };
type Story = { id: string; title: string; reason: string };
type Segment = { id: string; story_id?: string; text: string; fact_ids: string[]; sources: Source[]; conflict: boolean };
type Briefing = { id: string; profile_name: string; language: string; stories: Story[]; segments: Segment[]; audio_url: string | null; audio_status: string };
type Fact = { id: string; statement: string; value: string; unit: string; evidence: string; article_id: string; conflict_status: string };
type Evidence = { id: string; title: string; publication_date?: string; articles: Source[]; facts: Fact[] };
type Costs = { mode: string; stages: { stage: string; calls: number; input_tokens: number; output_tokens: number; characters: number; estimated_usd: number | null }[]; article_count: number; preparation_usd: number | null; per_briefing_usd: number | null; ten_x_preparation_usd: number | null };

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const res = await fetch(`/api${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  if (!res.ok) {
    const body = await res.text();
    let message = body;
    try {
      const payload: unknown = JSON.parse(body);
      if (typeof payload === 'object' && payload !== null) {
        const record = payload as { detail?: unknown; message?: unknown };
        const detail = record.detail ?? record.message;
        if (typeof detail === 'string') message = detail;
        else if (Array.isArray(detail)) message = detail.map(item => typeof item === 'object' && item !== null && 'msg' in item ? String(item.msg) : '').filter(Boolean).join('; ');
        else message = '';
      } else message = '';
    } catch { /* Keep a plain-text server message. */ }
    throw new Error(message && message.length < 300 ? message : `Request failed (${res.status})`);
  }
  try { return await res.json(); }
  catch { throw new Error('The server returned an unreadable response. Please try again.'); }
};
const errorMessage = (error: unknown) => {
  if (error instanceof Error && error.message === 'Failed to fetch') return 'The briefing service could not be reached. Check that it is running, then try again.';
  return error instanceof Error && error.message ? error.message : 'An unexpected error occurred. Please try again.';
};
const MAX_TOPICS = 12;
const languageNames = new Intl.DisplayNames(['en'], { type: 'language' });
const money = (n: number | null) => n === null ? 'Unavailable' : `$${n.toFixed(4)}`;

export default function App() {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [languages, setLanguages] = useState<string[]>(['en']);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [costs, setCosts] = useState<Costs | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [briefingSettings, setBriefingSettings] = useState('');
  const [mode, setMode] = useState<'profile' | 'wildcard'>('profile');
  const [profileId, setProfileId] = useState('');
  const [selectedTopics, setSelectedTopics] = useState<string[]>([]);
  const [expertise, setExpertise] = useState('intermediate');
  const [duration, setDuration] = useState(5);
  const [language, setLanguage] = useState('en');
  const [action, setAction] = useState<'briefing' | 'audio' | null>(null);
  const [briefingError, setBriefingError] = useState('');
  const [audioError, setAudioError] = useState('');
  const [failedPlayback, setFailedPlayback] = useState(false);
  const [audioRetryTime, setAudioRetryTime] = useState<number | null>(null);
  const [tab, setTab] = useState<'listen' | 'evidence' | 'costs'>('listen');
  const [playing, setPlaying] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [preferencesRequest, setPreferencesRequest] = useState(0);
  const [preferencesLoading, setPreferencesLoading] = useState(true);
  const [preferencesError, setPreferencesError] = useState('');
  const [evidenceRequest, setEvidenceRequest] = useState(0);
  const [evidenceLoading, setEvidenceLoading] = useState(true);
  const [evidenceError, setEvidenceError] = useState('');
  const [evidenceStoryId, setEvidenceStoryId] = useState<string | null>(null);
  const [costsLoading, setCostsLoading] = useState(true);
  const audio = useRef<HTMLAudioElement>(null);
  const briefingHeading = useRef<HTMLHeadingElement>(null);
  const focusBriefing = useRef(false);
  const evidenceHeading = useRef<HTMLHeadingElement>(null);
  const focusEvidence = useRef(false);
  const inspectButton = useRef<HTMLButtonElement | null>(null);
  const returnToStory = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    Promise.all([api<Profile[]>('/profiles', options), api<string[]>('/topics', options), api<string[]>('/languages', options)])
      .then(([profiles, topics, languages]) => {
        if (controller.signal.aborted) return;
        setProfiles(profiles);
        setProfileId(profiles[0]?.id ?? '');
        setTopics(topics);
        if (languages.length) {
          setLanguages(languages);
          setLanguage(languages[0]);
        }
      })
      .catch(error => {
        if (!controller.signal.aborted) setPreferencesError(`Could not load briefing settings. ${errorMessage(error)}`);
      })
      .finally(() => {
        if (!controller.signal.aborted) setPreferencesLoading(false);
      });
    return () => controller.abort();
  }, [preferencesRequest]);

  useEffect(() => {
    const controller = new AbortController();
    api<Evidence[]>('/evidence', { signal: controller.signal })
      .then(result => {
        if (!controller.signal.aborted) setEvidence(result);
      })
      .catch(error => {
        if (!controller.signal.aborted) setEvidenceError(`Could not load evidence. ${errorMessage(error)}`);
      })
      .finally(() => {
        if (!controller.signal.aborted) setEvidenceLoading(false);
      });
    return () => controller.abort();
  }, [evidenceRequest]);

  useEffect(() => {
    if (tab !== 'costs') return;
    const controller = new AbortController();
    setCostsLoading(true);
    api<Costs>('/costs', { signal: controller.signal })
      .then(setCosts)
      .catch(() => {
        if (!controller.signal.aborted) setCosts(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setCostsLoading(false);
      });
    return () => controller.abort();
  }, [tab, briefing]);

  useEffect(() => {
    if (focusBriefing.current && briefingHeading.current) {
      briefingHeading.current.focus();
      focusBriefing.current = false;
    }
  }, [briefing]);

  useEffect(() => {
    if (tab === 'evidence' && focusEvidence.current && evidenceHeading.current) {
      evidenceHeading.current.focus();
      focusEvidence.current = false;
    }
    if (tab === 'listen' && returnToStory.current) {
      inspectButton.current?.focus();
      returnToStory.current = false;
    }
  }, [tab, evidenceStoryId]);

  const activeProfile = profiles.find(profile => profile.id === profileId);
  const allFacts = useMemo(() => evidence.flatMap(item => item.facts), [evidence]);
  const canGenerate = mode === 'profile' ? Boolean(activeProfile) : selectedTopics.length > 0;
  const wildcard = { topics: selectedTopics, expertise, duration, language };
  const request = mode === 'profile' ? { profile_id: profileId } : { wildcard };
  const settingsKey = mode === 'profile'
    ? JSON.stringify(request)
    : JSON.stringify({ wildcard: { ...wildcard, topics: [...wildcard.topics].sort() } });
  const settingsChanged = briefingSettings !== '' && briefingSettings !== settingsKey;

  function changeView(view: typeof tab) {
    if (view !== 'listen') audio.current?.pause();
    setTab(view);
  }

  function inspectEvidence(storyId: string, button: HTMLButtonElement) {
    inspectButton.current = button;
    focusEvidence.current = true;
    setEvidenceStoryId(storyId);
    changeView('evidence');
  }

  async function makeBriefing() {
    audio.current?.pause();
    setAction('briefing');
    setBriefingError('');
    try {
      const result = await api<Briefing>('/briefings', { method: 'POST', body: JSON.stringify(request) });
      focusBriefing.current = true;
      setBriefing(result);
      setBriefingSettings(settingsKey);
      setEvidenceStoryId(null);
      inspectButton.current = null;
      setTab('listen');
      setExpanded(null);
      setPlaying(false);
      setFailedPlayback(false);
      setAudioRetryTime(null);
      setAudioError('');
    } catch (error) {
      setBriefingError(`Your briefing could not be prepared. ${errorMessage(error)}`);
    } finally {
      setAction(null);
    }
  }

  async function retryAudio() {
    if (!briefing) return;
    setAudioError('');
    setAction('audio');
    try {
      const result = await api<Briefing>(`/briefings/${briefing.id}/audio`, { method: 'POST' });
      setBriefing(result);
      if (result.audio_url) {
        // A regenerated file can have the same URL as the one that failed to load.
        setAudioRetryTime(Date.now());
      } else {
        setAudioError('Audio is still unavailable. You can keep reading or try again.');
      }
      setFailedPlayback(false);
    } catch (error) {
      setAudioError(`Audio is unavailable right now. ${errorMessage(error)}`);
    } finally {
      setAction(null);
    }
  }

  return <div className="app-shell">
    <header className="topbar"><a className="brand" href="#top" aria-label="Liquid Briefing home"><span className="brand-mark"><Waves size={18}/></span><span>liquid<span className="brand-light">briefing</span></span></a></header>
    <main id="top">
      <section className="intro"><div className="intro-title"><div className="eyebrow">PERSONAL AUDIO BRIEFING</div><h1>News worth <em>your attention.</em></h1></div><p className="intro-copy">Source-linked stories, voiced for your interests.</p></section>
      <div className="workspace">
        <aside className="setup-card" aria-label="Briefing preferences">
          <div className="card-heading"><div><span className="step-label">BRIEFING SETTINGS</span><h2>Choose your focus</h2></div></div>
          <div className="mode-switch" role="group" aria-label="Personalization mode">
            <button aria-pressed={mode === 'profile'} className={mode === 'profile' ? 'selected' : ''} onClick={() => setMode('profile')}>Listener profile</button>
            <button aria-pressed={mode === 'wildcard'} className={mode === 'wildcard' ? 'selected' : ''} onClick={() => setMode('wildcard')}>Custom briefing</button>
          </div>
          {preferencesLoading ? <LoadingRow text="Loading briefing settings…"/> : preferencesError ?
            <div className="preferences-error">
              <p role="alert">{preferencesError}</p>
              <button className="retry-audio" onClick={() => { setPreferencesLoading(true); setPreferencesError(''); setPreferencesRequest(value => value + 1); }}>
                <RotateCw size={14}/> Retry loading settings
              </button>
            </div> : mode === 'profile' ?
            <div className="form-section">
              <label htmlFor="profile">Choose a listener profile</label>
              <div className="select-wrap">
                <select id="profile" value={profileId} onChange={event => setProfileId(event.target.value)}>
                  {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
                </select>
                <ChevronDown size={16}/>
              </div>
              {activeProfile && <>
                {activeProfile.description && <p className="profile-description">{activeProfile.description}</p>}
                <div className="settings-row">
                  {activeProfile.duration && <span><Clock3 size={15}/>About {activeProfile.duration} minutes</span>}
                  {activeProfile.language && <span><AudioLines size={15}/>{languageNames.of(activeProfile.language)}</span>}
                  {activeProfile.expertise && <span><Sparkles size={15}/>{activeProfile.expertise}</span>}
                </div>
              </>}
            </div> :
            <div className="form-section wildcard-form">
              <fieldset className="topic-group" aria-describedby={topics.length ? 'topic-instructions' : undefined}>
                <legend>Topics to follow</legend>
                {topics.length > 0 ?
                  <>
                    <p className="microcopy" id="topic-instructions">{topics.length === 1 ? 'Select the available topic.' : `Choose 1 to ${Math.min(MAX_TOPICS, topics.length)} topics.`}</p>
                    <div className="topic-pills">
                      {topics.map(topic => {
                        const selected = selectedTopics.includes(topic);
                        return <button key={topic} className={selected ? 'topic-pill chosen' : 'topic-pill'}
                          aria-pressed={selected} disabled={!selected && selectedTopics.length >= MAX_TOPICS}
                          onClick={() => setSelectedTopics(current => selected ? current.filter(value => value !== topic) : [...current, topic])}>
                          {selected && <Check size={12} aria-hidden="true"/>}{topic}
                        </button>;
                      })}
                    </div>
                  </> : <p className="microcopy">No topics are available. Try a listener profile.</p>}
              </fieldset>
              <div className="inline-fields">
                <label>Depth<select value={expertise} onChange={event => setExpertise(event.target.value)}><option value="beginner">Beginner</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select></label>
                <label>Length<select value={duration} onChange={event => setDuration(Number(event.target.value))}><option value={3}>3 minutes</option><option value={5}>5 minutes</option><option value={8}>8 minutes</option></select></label>
              </div>
              <label>Language<select value={language} onChange={event => setLanguage(event.target.value)}>{languages.map(code => <option key={code} value={code}>{languageNames.of(code)}</option>)}</select></label>
            </div>}
          <button className="primary-button" onClick={makeBriefing} disabled={Boolean(action) || preferencesLoading || !canGenerate}>
            {action === 'briefing' ? <><LoaderCircle className="spin" size={17}/> Preparing your briefing</> : <><Sparkles size={16}/> Create my briefing <span className="button-arrow" aria-hidden="true">→</span></>}
          </button>
          <p className={settingsChanged && action !== 'briefing' ? 'microcopy' : 'visually-hidden'} role="status">
            {action === 'briefing' ? 'Preparing your briefing.' : settingsChanged ? 'Settings changed. Create a new briefing to apply them.' : ''}
          </p>
          {briefingError && <p className="action-error" role="alert">{briefingError}</p>}
          <div className="privacy-note"><ShieldCheck size={15}/><span>No account required.</span></div>
        </aside>
        <section className="content-card" aria-label="Briefing and sources">
          <div className="content-tabs">
            <nav className="tabs-left" aria-label="Briefing views">
              <button aria-pressed={tab === 'listen'} className={tab === 'listen' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('listen')}><Headphones size={15}/> Listen</button>
              <button aria-pressed={tab === 'evidence'} className={tab === 'evidence' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('evidence')}><BookOpen size={15}/> Evidence</button>
              <button aria-pressed={tab === 'costs'} className={tab === 'costs' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('costs')}><Info size={15}/> Costs</button>
            </nav>
          </div>
          <div hidden={tab !== 'listen'}>
            {briefing ?
              <BriefingView briefing={briefing} headingRef={briefingHeading} playing={playing} audioRef={audio}
                onPlaybackChange={setPlaying} onAudioError={() => { setFailedPlayback(true); setPlaying(false); }}
                audioRetryTime={audioRetryTime} failedPlayback={failedPlayback} audioError={audioError} expanded={expanded} onExpand={setExpanded}
                onRetry={retryAudio} busy={Boolean(action)} retrying={action === 'audio'} evidence={evidence}
                onInspectEvidence={inspectEvidence}/> :
              <div className="empty-state"><div className="sound-art" aria-hidden="true"><div/><div/><div/><AudioLines size={31}/></div><h2>No briefing yet</h2><p>Choose a profile or topics, then create your first audio briefing.</p></div>}
          </div>
          <div hidden={tab !== 'evidence'}>
            <EvidenceView evidence={evidence} facts={allFacts} loading={evidenceLoading} error={evidenceError}
              headingRef={evidenceHeading} storyId={evidenceStoryId}
              onBrowseAll={() => { focusEvidence.current = true; setEvidenceStoryId(null); }}
              onReturnToStory={() => { returnToStory.current = true; changeView('listen'); }}
              onRetry={() => { setEvidenceError(''); setEvidenceLoading(true); setEvidenceRequest(value => value + 1); }}/>
          </div>
          {tab === 'costs' && <CostsView costs={costs} loading={costsLoading}/>}
        </section>
      </div>
      <footer><span>LIQUID BRIEFING</span><span>Source links open original reporting.</span></footer>
    </main>
  </div>;
}

function BriefingView({ briefing, headingRef, playing, audioRef, onPlaybackChange, onAudioError, audioRetryTime, failedPlayback, audioError, expanded, onExpand, onRetry, busy, retrying, evidence, onInspectEvidence }: {
  briefing: Briefing;
  headingRef: RefObject<HTMLHeadingElement>;
  playing: boolean;
  audioRef: RefObject<HTMLAudioElement>;
  onPlaybackChange: (playing: boolean) => void;
  onAudioError: () => void;
  audioRetryTime: number | null;
  failedPlayback: boolean;
  audioError: string;
  expanded: string | null;
  onExpand: (segmentId: string | null) => void;
  onRetry: () => void;
  busy: boolean;
  retrying: boolean;
  evidence: Evidence[];
  onInspectEvidence: (storyId: string, button: HTMLButtonElement) => void;
}) {
  const audioSrc = briefing.audio_url && audioRetryTime
    ? `${briefing.audio_url}?retry=${audioRetryTime}`
    : briefing.audio_url;
  const ready = Boolean(audioSrc) && !failedPlayback;
  const unavailable = briefing.audio_status === 'unavailable';
  const storyCount = briefing.stories.length;
  return <div className="briefing-view">
    <div className="briefing-meta">
      <span className="status-chip"><span className={ready ? 'live-dot' : 'status-dot'}/>{ready ? 'Ready to listen' : 'Transcript ready'}</span>
      <span>{briefing.profile_name} <b>·</b> {languageNames.of(briefing.language)}</span>
    </div>
    <div className="briefing-title-row">
      <div><span className="step-label">YOUR PERSONAL BRIEFING</span><h2 className="briefing-heading" ref={headingRef} tabIndex={-1}>Today, in context.</h2></div>
      <span className="duration-pill">{storyCount} {storyCount === 1 ? 'story' : 'stories'}</span>
    </div>
    <div className={ready ? 'player ready' : 'player'}>
      <div className="player-info">
        <strong>{ready ? (playing ? 'Now playing' : 'Listen to your briefing') : 'Read your briefing below'}</strong>
        <span>{ready ? 'AI-generated narration' : failedPlayback ? 'The audio file could not be played.' : unavailable ? 'Audio generation is unavailable.' : 'Audio is ready to generate.'}</span>
      </div>
      {ready ?
        <audio key={audioSrc} ref={audioRef} className="audio-controls" controls preload="metadata"
          src={audioSrc ?? undefined} aria-label={`${briefing.profile_name} audio briefing`}
          onPlay={() => onPlaybackChange(true)} onPause={() => onPlaybackChange(false)}
          onEnded={() => onPlaybackChange(false)} onError={onAudioError}/> :
        <button className="retry-audio" onClick={onRetry} disabled={busy}>
          <RotateCw size={14}/>{retrying ? 'Generating audio…' : failedPlayback ? 'Reload audio' : unavailable ? 'Retry audio' : 'Generate audio'}
        </button>}
    </div>
    {(audioError || failedPlayback) && <p className="action-error" role="alert">{audioError || 'Playback failed. Reload the audio or keep reading.'}</p>}
    <div className="story-list">
      <div className="section-kicker"><span>IN THIS BRIEFING</span></div>
      {briefing.stories.map(story => {
        const storyEvidence = evidence.find(item => item.id === story.id);
        const segments = briefing.segments.filter(segment =>
          segment.story_id === story.id || segment.id === 'segment-' + story.id ||
          storyEvidence?.facts.some(fact => segment.fact_ids.includes(fact.id)));
        return <article className="story" key={story.id}>
          <div className="story-body">
            <h3>{story.title}</h3>
            <p>{story.reason}</p>
            <div className="transcript">
              {segments.map(segment => <div className="transcript-segment" key={segment.id}>
                <p>{segment.text}{segment.conflict && <span className="conflict-flag"><Info size={12}/> Conflicting reports</span>}</p>
                <button className="source-toggle" aria-expanded={expanded === segment.id} onClick={() => onExpand(expanded === segment.id ? null : segment.id)}>
                  <BookOpen size={13}/>{segment.sources.length} {segment.sources.length === 1 ? 'source' : 'sources'}
                  <ChevronDown size={13} className={expanded === segment.id ? 'rotated' : ''}/>
                </button>
                {expanded === segment.id && <div className="source-list">
                  {segment.sources.map(src => <a key={src.article_id} href={src.url} target="_blank" rel="noreferrer">
                    <span>{src.source}</span><strong>{src.title}</strong><span className="source-out">↗</span>
                  </a>)}
                </div>}
              </div>)}
            </div>
            {storyEvidence && <button className="source-toggle" aria-label={`Inspect evidence for ${story.title}`} onClick={event => onInspectEvidence(story.id, event.currentTarget)}>
              <BookOpen size={13}/> Inspect evidence
            </button>}
          </div>
        </article>;
      })}
    </div>
    <div className="voice-disclosure"><span className="voice-icon"><AudioLines size={16}/></span><p><strong>About this audio</strong><br/>The narration is AI-generated. Story selection and summaries are grounded in the cited reporting. Listen critically, especially where sources disagree.</p><BadgeCheck size={17} className="disclosure-check"/></div>
  </div>;
}

function EvidenceView({ evidence, facts, loading, error, headingRef, storyId, onBrowseAll, onReturnToStory, onRetry }: {
  evidence: Evidence[];
  facts: Fact[];
  loading: boolean;
  error: string;
  headingRef: RefObject<HTMLHeadingElement>;
  storyId: string | null;
  onBrowseAll: () => void;
  onReturnToStory: () => void;
  onRetry: () => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(20);
  const currentStory = evidence.find(item => item.id === storyId);
  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return evidence.filter(story => !term || [story.title, ...story.articles.map(article => article.title + ' ' + article.source)].some(value => value.toLowerCase().includes(term)))
      .sort((left, right) => right.articles.length - left.articles.length || (right.publication_date ?? '').localeCompare(left.publication_date ?? ''));
  }, [evidence, query]);
  const stories = currentStory ? [currentStory] : filtered.slice(0, limit);

  return <div className="data-view">
    <div className="data-heading">
      <span className="step-label">SHOW YOUR WORK</span>
      <h2 className="briefing-heading" ref={headingRef} tabIndex={-1}>{currentStory ? 'Evidence for this story' : 'Evidence, in the open.'}</h2>
      <p>Every briefing is built from reporting and extracted claims. Follow the links back to their sources.</p>
    </div>
    {loading ? <LoadingRow text="Loading evidence…"/> : error ?
      <div>
        <p className="action-error" role="alert">{error}</p>
        <button className="retry-audio" onClick={onRetry}><RotateCw size={14}/> Retry loading evidence</button>
      </div> : evidence.length === 0 ? <EmptyRow text="There are no prepared stories to inspect yet."/> : <>
        {currentStory ?
          <div className="evidence-actions">
            <button className="source-toggle" onClick={onReturnToStory}>Back to briefing story</button>
            <button className="source-toggle" onClick={onBrowseAll}>Browse all evidence</button>
          </div> :
          <>
            <div className="evidence-summary">
              <span><strong>{evidence.length}</strong> story clusters</span>
              <span><strong>{facts.length}</strong> extracted claims</span>
            </div>
            {evidence.length > 1 && <label className="evidence-search">Find a story or publisher
              <input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(20); }}/>
            </label>}
            {filtered.length === 0 && <EmptyRow text="No stories match your search."/>}
          </>}
        {stories.map(item => {
          const expanded = Boolean(currentStory) || open === item.id;
          return <article className="evidence-card" key={item.id}>
            {currentStory ?
              <h3 className="evidence-title"><strong>{item.title}</strong></h3> :
              <button className="evidence-title" onClick={() => setOpen(expanded ? null : item.id)} aria-expanded={expanded}>
                <span><span className="evidence-count">{item.articles.length} SOURCES</span><strong>{item.title}</strong></span>
                <ChevronDown size={17} className={expanded ? 'rotated' : ''}/>
              </button>}
            {expanded && <div className="evidence-detail">
              <div className="evidence-sources">
                {item.articles.map(article => <a key={article.article_id} href={article.url} target="_blank" rel="noreferrer">
                  <span>{article.source}</span><strong>{article.title}</strong><span>Open source ↗</span>
                </a>)}
              </div>
              {item.facts.length > 0 && <div className="fact-list">
                <span className="section-kicker">EXTRACTED CLAIMS</span>
                {item.facts.map(fact => <div className="fact" key={fact.id}>
                  <span className={['conflict', 'unresolved', 'needs_review'].includes(fact.conflict_status) ? 'fact-dot conflict' : 'fact-dot'}/>
                  <div><p>{fact.statement}</p>{(fact.value || fact.unit) && <span>{fact.value} {fact.unit}</span>}{fact.evidence && <blockquote>“{fact.evidence}”</blockquote>}</div>
                </div>)}
              </div>}
            </div>}
          </article>;
        })}
        {!currentStory && filtered.length > limit && <button className="retry-audio more-evidence" onClick={() => setLimit(value => value + 20)}>
          Show more stories ({limit} of {filtered.length})
        </button>}
      </>}
  </div>;
}
function CostsView({ costs, loading }: { costs: Costs | null; loading: boolean }) {
  return <div className="data-view"><div className="data-heading"><span className="step-label">TRANSPARENT BY DESIGN</span><h2>What does it cost?</h2><p>Provider usage and estimated costs for preparing stories and producing briefings.</p></div>{loading ? <LoadingRow/> : !costs ? <EmptyRow text="Cost estimates are not available right now."/> : <><div className="cost-hero"><div><span className="step-label">ESTIMATED PER BRIEFING</span><strong>{money(costs.per_briefing_usd)}</strong><span>average provider cost</span></div><div className="cost-icon"><AudioLines size={24}/></div></div><div className="cost-table-wrap"><table className="cost-table"><thead><tr><th>Stage</th><th>Calls</th><th>Tokens / chars</th><th>Est. cost</th></tr></thead><tbody>{costs.stages.map((s, i) => <tr key={`${s.stage}-${i}`}><td>{s.stage}</td><td>{s.calls}</td><td>{s.input_tokens + s.output_tokens > 0 ? `${(s.input_tokens + s.output_tokens).toLocaleString()} tokens` : `${s.characters.toLocaleString()} chars`}</td><td>{money(s.estimated_usd)}</td></tr>)}</tbody><tfoot><tr><td colSpan={3}>Per briefing total</td><td>{money(costs.per_briefing_usd)}</td></tr></tfoot></table></div><div className="cost-notes"><div><span className="step-label">PREPARATION</span><strong>{money(costs.preparation_usd)}</strong><p>Preparation across {costs.article_count.toLocaleString()} articles.</p></div><div><span className="step-label">10X ARTICLE VOLUME</span><strong>{money(costs.ten_x_preparation_usd)}</strong><p>Projected preparation cost at ten times the article volume.</p></div></div><div className="cost-footnote"><Info size={14}/><span>Speech calls count newly synthesized clips. Briefings can reuse them. Mode: {costs.mode}.</span></div></>}</div>;
}
function LoadingRow({ text = 'Loading data…' }: { text?: string }) { return <div className="data-loading" role="status"><LoaderCircle className="spin" size={18} aria-hidden="true"/> {text}</div>; }
function EmptyRow({ text }: { text: string }) { return <div className="data-loading" role="status">{text}</div>; }
