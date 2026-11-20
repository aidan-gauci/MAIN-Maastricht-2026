import { useEffect, useMemo, useRef, useState } from 'react';
import { AudioLines, BadgeCheck, BookOpen, Check, ChevronDown, CircleHelp, Clock3, Headphones, Info, LoaderCircle, RotateCw, ShieldCheck, Sparkles, ThumbsDown, ThumbsUp, Waves, X } from 'lucide-react';

type Profile = { id: string; name: string; description: string; topic_weights?: Record<string, number>; excluded_topics?: string[]; expertise?: string; language?: string; duration?: number; style?: string };
type Source = { article_id: string; title: string; source: string; url: string };
type Story = { id: string; title: string; reason: string };
type Segment = { id: string; story_id?: string; text: string; fact_ids: string[]; sources: Source[]; conflict: boolean };
type Briefing = { id: string; profile_name: string; language: string; stories: Story[]; segments: Segment[]; audio_url: string | null; audio_status: string; available_at?: string | null };
type VoicePreset = { id: string; label: string };
type Schedule = { schedule_id: string; schedule_version: number; profile_id: string; region: string; language: string; cadence: 'daily' | 'weekly'; local_time: string; weekday: number | null; timezone: string; voice_preset: string; generation_at: string; delivery_at: string; dispatch_status: string };
type ScheduleDraft = Omit<Schedule, 'schedule_id' | 'schedule_version' | 'generation_at' | 'delivery_at' | 'dispatch_status'>;
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
const money = (n: number | null) => n === null ? 'Unavailable' : `$${n.toFixed(4)}`;
const newListenerId = () => {
  const key = 'liquid-briefing-listener-id';
  const existing = localStorage.getItem(key);
  if (existing && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(existing)) return existing;
  const id = crypto.randomUUID();
  localStorage.setItem(key, id);
  return id;
};
const defaultTimezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const defaultScheduleDraft = (): ScheduleDraft => ({ profile_id: '', region: 'all', language: 'en', cadence: 'daily', local_time: '08:00', weekday: 0, timezone: defaultTimezone(), voice_preset: 'default' });
const toScheduleDraft = (schedule: Schedule): ScheduleDraft => ({ profile_id: schedule.profile_id, region: schedule.region, language: schedule.language, cadence: schedule.cadence, local_time: schedule.local_time, weekday: schedule.weekday, timezone: schedule.timezone, voice_preset: schedule.voice_preset });
const regionLabel = (region: string) => {
  if (region === 'all') return 'All regions';
  if (region === 'Europe') return 'Europe';
  try { return `${new Intl.DisplayNames(['en'], { type: 'region' }).of(region) ?? region} (${region})`; }
  catch { return region; }
};
const availableNow = (briefing: Briefing) => !briefing.available_at || Date.parse(briefing.available_at) <= Date.now();

export default function App() {
  const [listenerId] = useState(newListenerId);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [topics, setTopics] = useState<string[]>([]);
  const [languages, setLanguages] = useState<string[]>(['en']);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [costs, setCosts] = useState<Costs | null>(null);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [mode, setMode] = useState<'profile' | 'wildcard'>('profile');
  const [profileId, setProfileId] = useState('');
  const [selectedTopics, setSelectedTopics] = useState<string[]>([]);
  const [expertise, setExpertise] = useState('intermediate');
  const [duration, setDuration] = useState(5);
  const [language, setLanguage] = useState('en');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<'listen' | 'evidence' | 'costs'>('listen');
  const [playing, setPlaying] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [audio, setAudio] = useState<HTMLAudioElement | null>(null);
  const [loadingData, setLoadingData] = useState(true);
  const [regions, setRegions] = useState<string[]>(['all', 'Europe']);
  const [voicePresets, setVoicePresets] = useState<VoicePreset[]>([{ id: 'default', label: 'Default voice' }]);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [scheduleDraft, setScheduleDraft] = useState<ScheduleDraft>(defaultScheduleDraft);
  const scheduleDirtyRef = useRef(false);
  const scheduleResponseRef = useRef<Schedule | null>(null);
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [scheduleMessage, setScheduleMessage] = useState('');
  const [loadingListenerSettings, setLoadingListenerSettings] = useState(true);
  const [history, setHistory] = useState<Briefing[]>([]);
  const [ratings, setRatings] = useState<Record<string, 'up' | 'down'>>({});
  const [ratingBusy, setRatingBusy] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api<Profile[]>('/profiles'), api<string[]>('/topics'), api<string[]>('/languages'), api<Evidence[]>('/evidence'), api<Costs>('/costs')])
      .then(([p, t, l, e, c]) => {
        setProfiles(p); setProfileId(p[0]?.id ?? ''); setTopics(t); if (l.length) setLanguages(l); setEvidence(e); setCosts(c);
        if (!scheduleDirtyRef.current) {
          const initialLanguage = scheduleResponseRef.current?.language ?? (l.includes(language) ? language : l[0] ?? language);
          setLanguage(initialLanguage);
          setScheduleDraft(value => ({ ...value, profile_id: value.profile_id || p[0]?.id || '', language: initialLanguage }));
        } else {
          setScheduleDraft(value => ({ ...value, profile_id: value.profile_id || p[0]?.id || '' }));
        }
      })
      .catch(e => setError(`Could not load briefing data. ${errorMessage(e)}`)).finally(() => setLoadingData(false));
  }, []);

  async function refreshListenerData() {
    const [regionValues, presets, scheduleResponse, feedback, briefings] = await Promise.all([
      api<string[]>('/regions'), api<VoicePreset[]>('/voice-presets'),
      api<{ schedule: Schedule | null }>(`/listeners/${listenerId}/schedule`),
      api<{ ratings: Record<string, 'up' | 'down'> }>(`/listeners/${listenerId}/feedback`),
      api<Briefing[]>(`/listeners/${listenerId}/briefings`),
    ]);
    setRegions(regionValues);
    setVoicePresets(presets.length ? presets : [{ id: 'default', label: 'Default voice' }]);
    scheduleResponseRef.current = scheduleResponse.schedule;
    setSchedule(scheduleResponse.schedule);
    if (scheduleResponse.schedule && !scheduleDirtyRef.current) {
      setScheduleDraft(toScheduleDraft(scheduleResponse.schedule));
      setLanguage(scheduleResponse.schedule.language);
    }
    setRatings(feedback.ratings ?? {});
    setHistory(briefings);
    setBriefing(current => current ?? briefings[0] ?? null);
  }

  useEffect(() => {
    refreshListenerData()
      .catch(e => setScheduleMessage(`Listener settings could not load. ${errorMessage(e)}`))
      .finally(() => setLoadingListenerSettings(false));
    const refresh = () => { if (document.visibilityState === 'visible') refreshListenerData().catch(() => undefined); };
    const timer = window.setInterval(refresh, 60_000);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
  }, [listenerId]);

  useEffect(() => { if (tab === 'costs') api<Costs>('/costs').then(setCosts).catch(() => setCosts(null)); }, [tab, briefing]);

  const activeProfile = profiles.find(p => p.id === profileId);
  const allFacts = useMemo(() => evidence.flatMap(e => e.facts), [evidence]);
  const updateScheduleDraft = (update: (draft: ScheduleDraft) => ScheduleDraft) => {
    scheduleDirtyRef.current = true;
    setScheduleDraft(update);
  };
  async function makeBriefing() {
    setBusy(true); setError(''); setPlaying(false); audio?.pause();
    try {
      const request = { listener_id: listenerId, language, region: scheduleDraft.region, voice_preset: scheduleDraft.voice_preset };
      const data = mode === 'profile'
        ? await api<Briefing>('/briefings', { method: 'POST', body: JSON.stringify({ ...request, profile_id: profileId }) })
        : await api<Briefing>('/briefings', { method: 'POST', body: JSON.stringify({ ...request, wildcard: { topics: selectedTopics, expertise, duration, language } }) });
      setBriefing(data); setTab('listen'); setExpanded(null);
    } catch (e) { setError(`Your briefing could not be prepared. ${errorMessage(e)}`); }
    finally { setBusy(false); }
  }
  async function saveSchedule() {
    setScheduleBusy(true); setScheduleMessage('');
    try {
      const saved = await api<{ schedule: Schedule }>(`/listeners/${listenerId}/schedule`, {
        method: 'PUT',
        body: JSON.stringify({
          profile_id: scheduleDraft.profile_id,
          region: scheduleDraft.region,
          language: scheduleDraft.language,
          cadence: scheduleDraft.cadence,
          local_time: scheduleDraft.local_time,
          weekday: scheduleDraft.cadence === 'weekly' ? scheduleDraft.weekday : null,
          timezone: scheduleDraft.timezone,
          voice_preset: scheduleDraft.voice_preset,
        }),
      });
      scheduleResponseRef.current = saved.schedule;
      setSchedule(saved.schedule);
      setScheduleDraft(toScheduleDraft(saved.schedule));
      scheduleDirtyRef.current = false;
      setScheduleMessage('Delivery schedule saved.');
    } catch (e) { setScheduleMessage(`Schedule could not be saved. ${errorMessage(e)}`); }
    finally { setScheduleBusy(false); }
  }
  async function rateStory(storyId: string, rating: 'up' | 'down') {
    setRatingBusy(storyId);
    try {
      await api<{ rating: 'up' | 'down' }>(`/listeners/${listenerId}/feedback/${encodeURIComponent(storyId)}`, { method: 'PUT', body: JSON.stringify({ rating }) });
      setRatings(value => ({ ...value, [storyId]: rating }));
    } catch (e) { setError(`Feedback could not be saved. ${errorMessage(e)}`); }
    finally { setRatingBusy(null); }
  }
  async function retryAudio() {
    if (!briefing) return; setBusy(true); setError('');
    try { const data = await api<Briefing>(`/briefings/${encodeURIComponent(briefing.id)}/audio?listener_id=${encodeURIComponent(listenerId)}`, { method: 'POST' }); setBriefing(data); }
    catch (e) { setError(`Audio is unavailable right now. ${errorMessage(e)}`); }
    finally { setBusy(false); }
  }
  return <div className="app-shell">
    <header className="topbar"><a className="brand" href="#top" aria-label="Liquid Briefing home"><span className="brand-mark"><Waves size={18}/></span><span>liquid<span className="brand-light">briefing</span></span></a></header>
    <main id="top">
      <section className="intro"><div className="intro-title"><div className="eyebrow">PERSONAL AUDIO BRIEFING</div><h1>News worth <em>your attention.</em></h1></div><p className="intro-copy">Source-linked stories, voiced for your interests.</p></section>
      <div className="workspace">
        <aside className="setup-card" aria-label="Briefing preferences"><div className="card-heading"><div><span className="step-label">BRIEFING SETTINGS</span><h2>Choose your focus</h2></div></div>
          <div className="mode-switch" role="tablist" aria-label="Personalization mode"><button role="tab" aria-selected={mode === 'profile'} className={mode === 'profile' ? 'selected' : ''} onClick={() => setMode('profile')}>My profile</button><button role="tab" aria-selected={mode === 'wildcard'} className={mode === 'wildcard' ? 'selected' : ''} onClick={() => setMode('wildcard')}>Make it yours</button></div>
          {mode === 'profile' ? <div className="form-section"><label htmlFor="profile">Choose a listener profile</label><div className="select-wrap"><select id="profile" value={profileId} onChange={e => setProfileId(e.target.value)} disabled={loadingData || !profiles.length}><option value="">{loadingData ? 'Loading profiles…' : 'Select a profile'}</option>{profiles.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select><ChevronDown size={16}/></div>{activeProfile && <div className="profile-summary"><span className="profile-avatar">{activeProfile.name.slice(0, 1)}</span><div><strong>{activeProfile.name}</strong><p>{activeProfile.description}</p></div></div>}<p className="microcopy">A profile tunes topic balance, pace and depth.</p></div> : <div className="form-section wildcard-form"><label>Topics to follow <span className="muted">(choose a few)</span></label><div className="topic-pills">{topics.map(topic => <button key={topic} className={selectedTopics.includes(topic) ? 'topic-pill chosen' : 'topic-pill'} onClick={() => setSelectedTopics(v => v.includes(topic) ? v.filter(x => x !== topic) : [...v, topic])} aria-pressed={selectedTopics.includes(topic)}>{selectedTopics.includes(topic) && <Check size={12}/>} {topic}</button>)}</div><div className="inline-fields"><label>Depth<select value={expertise} onChange={e => setExpertise(e.target.value)}><option value="beginner">Beginner</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select></label><label>Length<select value={duration} onChange={e => setDuration(Number(e.target.value))}><option value={3}>3 minutes</option><option value={5}>5 minutes</option><option value={8}>8 minutes</option></select></label></div></div>}
          {mode === 'profile' && <div className="settings-row"><span><Clock3 size={15}/>About {activeProfile?.duration ?? 5} minutes</span><span><AudioLines size={15}/>{language.toUpperCase()}</span><span><Sparkles size={15}/>{activeProfile?.expertise || 'Balanced'}</span></div>}
          <div className="delivery-settings">
            <div className="delivery-heading"><span className="step-label">DELIVERY SETTINGS</span><p>Choose how your briefing should be delivered.</p></div>
            <label>Output language<select value={language} disabled={loadingListenerSettings} onChange={e => { setLanguage(e.target.value); updateScheduleDraft(v => ({ ...v, language: e.target.value })); }}>{languages.map(value => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select></label>
            <label>Region<select value={scheduleDraft.region} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, region: e.target.value }))}>{regions.map(value => <option key={value} value={value}>{regionLabel(value)}</option>)}</select></label>
            <label>Voice preset<select value={scheduleDraft.voice_preset} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, voice_preset: e.target.value }))}>{voicePresets.map(preset => <option key={preset.id} value={preset.id}>{preset.label}</option>)}</select></label>
          </div>
          <details className="schedule-settings">
            <summary><span><Clock3 size={15}/>Schedule delivery</span><ChevronDown size={15}/></summary>
            <div className="schedule-fields">
              <label>Profile<select value={scheduleDraft.profile_id || profileId} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, profile_id: e.target.value }))}>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>
              <label>Cadence<select value={scheduleDraft.cadence} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, cadence: e.target.value as ScheduleDraft['cadence'] }))}><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label>
              {scheduleDraft.cadence === 'weekly' && <label>Weekday<select value={scheduleDraft.weekday ?? 0} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, weekday: Number(e.target.value) }))}>{['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, index) => <option key={day} value={index}>{day}</option>)}</select></label>}
              <label>Local delivery time<input type="time" value={scheduleDraft.local_time} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, local_time: e.target.value }))}/></label>
              <label>Time zone (IANA)<input value={scheduleDraft.timezone} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, timezone: e.target.value }))} placeholder="Europe/Amsterdam" autoComplete="off"/></label>
              <p className="schedule-note">Generation starts 30 minutes before delivery. Your audio appears at the selected time.</p>
              <button className="retry-audio save-schedule" onClick={saveSchedule} disabled={scheduleBusy || loadingData || loadingListenerSettings || !scheduleDraft.profile_id}>{scheduleBusy ? 'Saving schedule…' : schedule ? 'Save schedule changes' : 'Save delivery schedule'}</button>
              {scheduleMessage && <p className="schedule-message" role="status">{scheduleMessage}</p>}
              {schedule && <><p className={/fail|error|unavailable/i.test(schedule.dispatch_status) ? 'schedule-message schedule-failed' : 'schedule-message'} role="status">Scheduler status: {schedule.dispatch_status.replace(/_/g, ' ')}</p><p className="next-delivery">Next delivery {new Date(schedule.delivery_at).toLocaleString([], { timeZone: schedule.timezone, dateStyle: 'medium', timeStyle: 'short' })} ({schedule.timezone})</p></>}
            </div>
          </details>
          <button className="primary-button" onClick={makeBriefing} disabled={busy || loadingData || (mode === 'profile' ? !profileId : !selectedTopics.length)}>{busy ? <><LoaderCircle className="spin" size={17}/> Preparing your briefing</> : <><Sparkles size={16}/> Create my briefing <span className="button-arrow">→</span></>}</button>
        <div className="privacy-note"><ShieldCheck size={15}/><span>No account. Your browser ID keeps your schedule and feedback together.</span></div>
        </aside>
        <section className="content-card" aria-live="polite">
          <div className="content-tabs"><nav className="tabs-left" aria-label="Briefing views"><button aria-pressed={tab === 'listen'} className={tab === 'listen' ? 'content-tab active' : 'content-tab'} onClick={() => setTab('listen')}><Headphones size={15}/> Listen</button><button aria-pressed={tab === 'evidence'} className={tab === 'evidence' ? 'content-tab active' : 'content-tab'} onClick={() => setTab('evidence')}><BookOpen size={15}/> Evidence</button><button aria-pressed={tab === 'costs'} className={tab === 'costs' ? 'content-tab active' : 'content-tab'} onClick={() => setTab('costs')}><Info size={15}/> Model</button></nav></div>
          {error && <div role="alert" className="error-banner"><CircleHelp size={16}/><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={15}/></button></div>}
          {tab === 'listen' && <>{history.length > 0 && <div className="briefing-history"><span className="section-kicker">DELIVERED BRIEFINGS</span>{history.map(item => <button key={item.id} className={briefing?.id === item.id ? 'history-item selected' : 'history-item'} onClick={() => setBriefing(item)}><span>{item.profile_name} · {item.language.toUpperCase()}</span><strong>{item.stories.length} stories</strong></button>)}</div>}{briefing ? <BriefingView briefing={briefing} playing={playing} onAudioElement={setAudio} onPlaybackChange={setPlaying} onAudioError={() => { setError('The audio file could not be played. Try generating it again.'); setPlaying(false); }} expanded={expanded} onExpand={setExpanded} onRetry={retryAudio} busy={busy} evidence={evidence} ratings={ratings} ratingBusy={ratingBusy} onRate={rateStory}/> : <div className="empty-state"><div className="sound-art"><div/><div/><div/><AudioLines size={31}/></div><h2>No briefing yet</h2><p>Choose a profile or topics, then create your first audio briefing.</p></div>}</>}
          {tab === 'evidence' && <EvidenceView evidence={evidence} facts={allFacts} loading={loadingData}/>}
          {tab === 'costs' && <CostsView costs={costs} loading={loadingData}/>}
        </section>
      </div>
      <footer><span>LIQUID BRIEFING</span><span>Source links open original reporting.</span></footer>
    </main>
  </div>;
}

function BriefingView({ briefing, playing, onAudioElement, onPlaybackChange, onAudioError, expanded, onExpand, onRetry, busy, evidence, ratings, ratingBusy, onRate }: { briefing: Briefing; playing: boolean; onAudioElement: (audio: HTMLAudioElement | null) => void; onPlaybackChange: (playing: boolean) => void; onAudioError: () => void; expanded: string | null; onExpand: (s: string | null) => void; onRetry: () => void; busy: boolean; evidence: Evidence[]; ratings: Record<string, 'up' | 'down'>; ratingBusy: string | null; onRate: (storyId: string, rating: 'up' | 'down') => void }) {
  const status = (briefing.audio_status || '').toLowerCase(); const available = availableNow(briefing); const ready = Boolean(briefing.audio_url) && available; const audioError = status === 'unavailable';
  const usedSources = [...new Map(briefing.segments.flatMap(segment => segment.sources).map(source => [source.article_id, source])).values()];
  return <div className="briefing-view"><div className="briefing-meta"><span className="status-chip"><span className={ready ? 'live-dot' : 'status-dot'}/>{ready ? 'READY TO LISTEN' : status ? status.replace(/_/g, ' ').toUpperCase() : 'BRIEFING PREPARED'}</span><span>{briefing.profile_name} <b>·</b> {briefing.language.toUpperCase()}</span></div><div className="briefing-title-row"><div><span className="step-label">YOUR PERSONAL BRIEFING</span><h2>Today, in context.</h2></div><span className="duration-pill"><Clock3 size={14}/>{briefing.stories.length} stories</span></div>
    <div className={ready ? 'player ready' : 'player'}><div className="player-info"><strong>{ready ? (playing ? 'Now playing' : 'Listen to your briefing') : available ? 'Your briefing is ready to read' : 'Your briefing is scheduled'}</strong><span>{ready ? 'AI-generated narration' : !available ? `Audio will be available ${new Date(briefing.available_at!).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}.` : audioError ? 'Audio generation needs another try.' : 'Audio is being prepared when available.'}</span></div>{ready ? <audio key={briefing.audio_url} ref={onAudioElement} className="audio-controls" controls preload="metadata" src={briefing.audio_url ?? undefined} aria-label={`${briefing.profile_name} audio briefing`} onPlay={() => onPlaybackChange(true)} onPause={() => onPlaybackChange(false)} onEnded={() => onPlaybackChange(false)} onError={onAudioError}/> : available && <button className="retry-audio" onClick={onRetry} disabled={busy}><RotateCw size={14}/>{busy ? 'Retrying' : audioError ? 'Retry audio' : 'Generate audio'}</button>}</div>
    {ready && <section className="briefing-sources" aria-label="Sources used by this briefing"><div><span className="step-label">SOURCES USED</span><p>Reporting cited in this briefing.</p></div>{usedSources.length ? <div className="briefing-source-list">{usedSources.map(source => <a key={source.article_id} href={source.url} target="_blank" rel="noreferrer"><span>{source.source}</span><strong>{source.title}</strong></a>)}</div> : <p className="no-sources">No source links were attached to this briefing.</p>}</section>}
    <div className="story-list"><div className="section-kicker"><span>IN THIS BRIEFING</span><span>{briefing.stories.length} stories</span></div>{briefing.stories.map(story => <article className="story" key={story.id}><div className="story-body"><h3>{story.title}</h3><p>{story.reason}</p><div className="story-feedback" aria-label={`Rate ${story.title}`}><span>Was this useful?</span><button type="button" className={ratings[story.id] === 'up' ? 'feedback-button chosen' : 'feedback-button'} onClick={() => onRate(story.id, 'up')} disabled={busy || ratingBusy === story.id} aria-label={`Mark ${story.title} useful`} aria-pressed={ratings[story.id] === 'up'}><ThumbsUp size={14}/> Helpful</button><button type="button" className={ratings[story.id] === 'down' ? 'feedback-button chosen' : 'feedback-button'} onClick={() => onRate(story.id, 'down')} disabled={busy || ratingBusy === story.id} aria-label={`Mark ${story.title} not useful`} aria-pressed={ratings[story.id] === 'down'}><ThumbsDown size={14}/> Not useful</button></div><div className="transcript">{briefing.segments.filter(s => s.story_id === story.id || s.id === 'segment-' + story.id || s.fact_ids.some(fid => evidence.some(ev => ev.facts.some(f => f.id === fid && ev.id === story.id)))).map(seg => <div className="transcript-segment" key={seg.id}><p>{seg.text}{seg.conflict && <span className="conflict-flag"><Info size={12}/> Conflicting reports</span>}</p>{available && <><button className="source-toggle" aria-expanded={expanded === seg.id} onClick={() => onExpand(expanded === seg.id ? null : seg.id)}><BookOpen size={13}/>{seg.sources.length} source{seg.sources.length === 1 ? '' : 's'}<ChevronDown size={13} className={expanded === seg.id ? 'rotated' : ''}/></button>{expanded === seg.id && <div className="source-list">{seg.sources.map(src => <a key={src.article_id} href={src.url} target="_blank" rel="noreferrer"><span>{src.source}</span><strong>{src.title}</strong><span className="source-out">↗</span></a>)}</div>}</>}</div>)}</div></div></article>)}</div>
    <div className="voice-disclosure"><span className="voice-icon"><AudioLines size={16}/></span><p><strong>About this audio</strong><br/>The narration is AI-generated. Story selection and summaries are grounded in the cited reporting. Listen critically, especially where sources disagree.</p><BadgeCheck size={17} className="disclosure-check"/></div>
  </div>;
}

function EvidenceView({ evidence, facts, loading }: { evidence: Evidence[]; facts: Fact[]; loading: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(20);
  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return evidence.filter(story => !term || [story.title, ...story.articles.map(article => article.title + ' ' + article.source)].some(value => value.toLowerCase().includes(term)))
      .sort((left, right) => right.articles.length - left.articles.length || (right.publication_date ?? '').localeCompare(left.publication_date ?? ''));
  }, [evidence, query]);
  return <div className="data-view"><div className="data-heading"><span className="step-label">SHOW YOUR WORK</span><h2>Evidence, in the open.</h2><p>Every briefing is built from reporting and extracted claims. Follow the links back to their sources.</p></div>{loading ? <LoadingRow/> : evidence.length === 0 ? <EmptyRow text="Evidence data is not available yet."/> : <><div className="evidence-summary"><span><strong>{evidence.length}</strong> story clusters</span><span><strong>{facts.length}</strong> extracted claims</span></div>{evidence.length > 20 && <label className="evidence-search">Find a story or publisher<input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(20); }} /></label>}{filtered.length === 0 && <EmptyRow text="No stories match your search."/>}{filtered.slice(0, limit).map(item => <article className="evidence-card" key={item.id}><button className="evidence-title" onClick={() => setOpen(open === item.id ? null : item.id)} aria-expanded={open === item.id}><span><span className="evidence-count">{item.articles.length} SOURCES</span><strong>{item.title}</strong></span><ChevronDown size={17} className={open === item.id ? 'rotated' : ''}/></button>{open === item.id && <div className="evidence-detail"><div className="evidence-sources">{item.articles.map(a => <a key={a.article_id} href={a.url} target="_blank" rel="noreferrer"><span>{a.source}</span><strong>{a.title}</strong><span>Open source ↗</span></a>)}</div>{item.facts.length > 0 && <div className="fact-list"><span className="section-kicker">EXTRACTED CLAIMS</span>{item.facts.map(f => <div className="fact" key={f.id}><span className={['conflict', 'unresolved', 'needs_review'].includes(f.conflict_status) ? 'fact-dot conflict' : 'fact-dot'}/><div><p>{f.statement}</p>{(f.value || f.unit) && <span>{f.value} {f.unit}</span>}{f.evidence && <blockquote>“{f.evidence}”</blockquote>}</div></div>)}</div>}</div>}</article>)}{filtered.length > limit && <button className="retry-audio more-evidence" onClick={() => setLimit(value => value + 20)}>Show more stories ({limit} of {filtered.length})</button>}</>}</div>;
}
function CostsView({ costs, loading }: { costs: Costs | null; loading: boolean }) {
  return <div className="data-view"><div className="data-heading"><span className="step-label">TRANSPARENT BY DESIGN</span><h2>What does it cost?</h2><p>Provider usage and estimated costs for preparing stories and producing briefings.</p></div>{loading ? <LoadingRow/> : !costs ? <EmptyRow text="Cost estimates are not available right now."/> : <><div className="cost-hero"><div><span className="step-label">ESTIMATED PER BRIEFING</span><strong>{money(costs.per_briefing_usd)}</strong><span>for each generated briefing</span></div><div className="cost-icon"><AudioLines size={24}/></div></div><div className="cost-table-wrap"><table className="cost-table"><thead><tr><th>Stage</th><th>Calls</th><th>Tokens / chars</th><th>Est. cost</th></tr></thead><tbody>{costs.stages.map((s, i) => <tr key={`${s.stage}-${i}`}><td>{s.stage}</td><td>{s.calls}</td><td>{s.input_tokens + s.output_tokens > 0 ? `${(s.input_tokens + s.output_tokens).toLocaleString()} tokens` : `${s.characters.toLocaleString()} chars`}</td><td>{money(s.estimated_usd)}</td></tr>)}</tbody><tfoot><tr><td colSpan={3}>Per briefing total</td><td>{money(costs.per_briefing_usd)}</td></tr></tfoot></table></div><div className="cost-notes"><div><span className="step-label">PREPARATION</span><strong>{money(costs.preparation_usd)}</strong><p>Preparation across {costs.article_count.toLocaleString()} articles.</p></div><div><span className="step-label">10X ARTICLE VOLUME</span><strong>{money(costs.ten_x_preparation_usd)}</strong><p>Projected preparation cost at ten times the article volume.</p></div></div><div className="cost-footnote"><Info size={14}/><span>Estimates are indicative and depend on the models and audio length used. Mode: {costs.mode}.</span></div></>}</div>;
}
function LoadingRow() { return <div className="data-loading" role="status" aria-live="polite"><LoaderCircle className="spin" size={18} aria-hidden="true"/> Loading data…</div>; }
function EmptyRow({ text }: { text: string }) { return <div className="data-loading" role="status">{text}</div>; }
