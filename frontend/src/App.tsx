import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { AudioLines, BookOpen, Check, ChevronDown, CircleAlert, Clock3, Gauge, Headphones, Info, Languages, Layers, LoaderCircle, Newspaper, Play, RotateCw, ShieldCheck, Square, ThumbsDown, ThumbsUp, Waypoints, X } from 'lucide-react';
import { TracesView, type TraceFocus } from './traces';
import { count, eventSummary, eventThreads, hasMergeBadges, joinList, mergeInfo, shortDate, threadStats, type EventThread, type Merge } from './events';
import { ThemeToggle } from './theme';
import logoForLight from './assets/brand/maze-logo-for-light-mode.svg';
import logoForDark from './assets/brand/maze-logo-for-dark-mode.svg';

type Profile = { id: string; name: string; description: string; topic_weights?: Record<string, number>; excluded_topics?: string[]; expertise?: string; language?: string; duration?: number; style?: string };
type Source = { article_id: string; title: string; source: string; url: string; language?: string };
type Story = { id: string; title: string; reason: string; title_language?: string; source_title?: string };
type Segment = { id: string; story_id?: string; text: string; fact_ids: string[]; sources: Source[]; conflict: boolean; refresher_text?: string; refresher_fact_ids?: string[] };
type Briefing = { id: string; profile_name: string; language: string; stories: Story[]; segments: Segment[]; audio_url: string | null; audio_status: string; available_at?: string | null; estimated_seconds?: number; requested_minutes?: number };
type VoicePreset = { id: string; label: string; description?: string; preview?: boolean; languages?: string[] | null };
// Every voice speaks every output language; ElevenLabs has verified only some, and elsewhere the accent may vary.
const englishLanguageName = (code: string) => { try { return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) ?? code; } catch { return code; } };
const voiceVerifiedFor = (preset: VoicePreset | undefined, code: string) => !preset?.languages || preset.languages.includes(code);
const voiceOptionLabel = (preset: VoicePreset, code: string) => {
  const details = [preset.description, voiceVerifiedFor(preset, code) ? '' : `accent may vary in ${englishLanguageName(code)}`].filter(Boolean).join('; ');
  return details ? `${preset.label} (${details})` : preset.label;
};
type ScheduleSelection = { topics: string[]; expertise: string; duration: number; language: string };
type Schedule = { schedule_id: string; schedule_version: number; profile_id: string; wildcard: ScheduleSelection | null; region: string; language: string; cadence: 'daily' | 'weekly'; local_time: string; weekday: number | null; timezone: string; voice_preset: string; generation_at: string; delivery_at: string; dispatch_status: string };
type ScheduleDraft = Omit<Schedule, 'schedule_id' | 'schedule_version' | 'generation_at' | 'delivery_at' | 'dispatch_status'>;
type Fact = { id: string; statement: string; value: string; unit: string; evidence: string; article_id: string; conflict_status: string };
// thread_id and thread_title are present only on stories that belong to an event thread of two or more stories.
type Evidence = { id: string; title: string; publication_date?: string; articles: Source[]; facts: Fact[]; thread_id?: string; thread_title?: string };
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
// Each language is named in itself ("Deutsch", "Nederlands"), so a listener finds theirs whatever the page is written in.
const nativeLanguageNames: Record<string, string> = { en: 'English', de: 'Deutsch', nl: 'Nederlands' };
const languageLabel = (code: string) => {
  if (nativeLanguageNames[code]) return nativeLanguageNames[code];
  try { return new Intl.DisplayNames([code], { type: 'language' }).of(code) ?? code.toUpperCase(); } catch { return code.toUpperCase(); }
};
const money = (n: number | null) => n === null ? 'Unavailable' : `$${n.toFixed(4)}`;
const newListenerId = () => {
  const key = 'briefing-listener-id';
  const existing = localStorage.getItem(key);
  if (existing && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(existing)) return existing;
  const id = crypto.randomUUID();
  localStorage.setItem(key, id);
  return id;
};
// Per-browser conveniences; storage can be unavailable (private windows), so reads and writes never throw.
const readSetting = (key: string) => { try { return localStorage.getItem(key); } catch { return null; } };
const writeSetting = (key: string, value: string) => { try { localStorage.setItem(key, value); } catch { /* ignore */ } };
// Speed is applied by the browser to the existing file, so changing it never re-generates audio.
const PLAYBACK_SPEEDS = [0.8, 1, 1.2, 1.5];
const SPEED_KEY = 'briefing-playback-speed';
const storedSpeed = () => { const value = Number(readSetting(SPEED_KEY)); return PLAYBACK_SPEEDS.includes(value) ? value : 1; };
const applySpeed = (element: HTMLAudioElement | null, speed: number) => {
  if (!element) return;
  element.preservesPitch = true;
  element.defaultPlaybackRate = speed;
  element.playbackRate = speed;
};
// Scheduled delivery needs the n8n workflows, a public HTTPS backend and durable storage; hide it unless explicitly enabled.
const SHOW_SCHEDULE = import.meta.env.VITE_SHOW_SCHEDULE !== '0';
const defaultTimezone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const CUSTOM_SCHEDULE = '__custom';
const defaultScheduleDraft = (): ScheduleDraft => ({ profile_id: '', wildcard: null, region: 'all', language: 'en', cadence: 'daily', local_time: '08:00', weekday: 0, timezone: defaultTimezone(), voice_preset: 'default' });
const toScheduleDraft = (schedule: Schedule): ScheduleDraft => ({ profile_id: schedule.profile_id, wildcard: schedule.wildcard ?? null, region: schedule.region, language: schedule.language, cadence: schedule.cadence, local_time: schedule.local_time, weekday: schedule.weekday, timezone: schedule.timezone, voice_preset: schedule.voice_preset });
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
  const [tab, setTab] = useState<'listen' | 'evidence' | 'traces' | 'costs'>('listen');
  const [traceFocus, setTraceFocus] = useState<TraceFocus | null>(null);
  const [playing, setPlaying] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [regions, setRegions] = useState<string[]>(['all']);
  const [voicePresets, setVoicePresets] = useState<VoicePreset[]>([{ id: 'default', label: 'Default voice' }]);
  // A voice's own sample from ElevenLabs; playing it uses no credits.
  const voiceSample = useRef<HTMLAudioElement | null>(null);
  const [sampleState, setSampleState] = useState<'idle' | 'loading' | 'playing'>('idle');
  const [sampleError, setSampleError] = useState('');
  function stopVoiceSample() {
    voiceSample.current?.pause();
    voiceSample.current = null;
    setSampleState('idle');
    setSampleError('');
  }
  function toggleVoiceSample() {
    if (sampleState !== 'idle') { stopVoiceSample(); return; }
    audio.current?.pause();
    const sample = new Audio(`/api/voice-presets/${encodeURIComponent(scheduleDraft.voice_preset)}/preview`);
    voiceSample.current = sample;
    setSampleError('');
    setSampleState('loading');
    sample.onplaying = () => setSampleState('playing');
    sample.onended = () => { voiceSample.current = null; setSampleState('idle'); };
    sample.onerror = () => { voiceSample.current = null; setSampleState('idle'); setSampleError('The voice sample could not be played. You can still create a briefing with this voice.'); };
    sample.play().catch(() => undefined);
  }
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
  const traceButton = useRef<HTMLButtonElement | null>(null);
  const returnToTrace = useRef(false);
  const traceRequests = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const options = { signal: controller.signal };
    Promise.all([api<Profile[]>('/profiles', options), api<string[]>('/topics', options), api<string[]>('/languages', options)])
      .then(([profiles, topics, languages]) => {
        if (controller.signal.aborted) return;
        setProfiles(profiles);
        setProfileId(profiles[0]?.id ?? '');
        setTopics(topics);
        if (languages.length) setLanguages(languages);
        if (!scheduleDirtyRef.current) {
          const initialLanguage = scheduleResponseRef.current?.language ?? (languages.includes(language) ? language : languages[0] ?? language);
          setLanguage(initialLanguage);
          setScheduleDraft(value => ({ ...value, profile_id: value.profile_id || profiles[0]?.id || '', language: initialLanguage }));
        } else {
          setScheduleDraft(value => ({ ...value, profile_id: value.profile_id || profiles[0]?.id || '' }));
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
    if (tab === 'listen' && returnToTrace.current) {
      traceButton.current?.focus();
      returnToTrace.current = false;
    }
  }, [tab, evidenceStoryId]);

  const activeProfile = profiles.find(profile => profile.id === profileId);
  const allFacts = useMemo(() => evidence.flatMap(item => item.facts), [evidence]);
  const canGenerate = mode === 'profile' ? Boolean(activeProfile) : selectedTopics.length > 0;
  const wildcard = { topics: selectedTopics, expertise, duration, language };
  const delivery = { listener_id: listenerId, language, region: scheduleDraft.region, voice_preset: scheduleDraft.voice_preset };
  const request = mode === 'profile' ? { ...delivery, profile_id: profileId } : { ...delivery, wildcard };
  const settingsKey = mode === 'profile'
    ? JSON.stringify(request)
    : JSON.stringify({ wildcard: { ...wildcard, topics: [...wildcard.topics].sort() } });
  const settingsChanged = briefingSettings !== '' && briefingSettings !== settingsKey;

  function updateScheduleDraft(update: (draft: ScheduleDraft) => ScheduleDraft) {
    scheduleDirtyRef.current = true;
    setScheduleDraft(update);
  }

  function changeView(view: typeof tab) {
    if (view !== 'listen') audio.current?.pause();
    if (view !== 'traces') setTraceFocus(null);
    setTab(view);
  }

  function inspectTrace(segmentId: string, button: HTMLButtonElement) {
    traceButton.current = button;
    traceRequests.current += 1;
    setTraceFocus({ segmentId, nonce: traceRequests.current });
    changeView('traces');
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
      setTraceFocus(null);
      traceButton.current = null;
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

  async function saveSchedule() {
    setScheduleBusy(true); setScheduleMessage('');
    try {
      const saved = await api<{ schedule: Schedule }>(`/listeners/${listenerId}/schedule`, {
        method: 'PUT',
        body: JSON.stringify({
          // A schedule delivers a saved profile or the listener's own selection, in the schedule's language.
          ...(scheduleDraft.wildcard ? { wildcard: { ...scheduleDraft.wildcard, language: scheduleDraft.language } } : { profile_id: scheduleDraft.profile_id }),
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
    if (!briefing) return;
    setAudioError('');
    setAction('audio');
    try {
      const result = await api<Briefing>(`/briefings/${encodeURIComponent(briefing.id)}/audio?listener_id=${encodeURIComponent(listenerId)}`, { method: 'POST' });
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
    <header className="topbar"><div className="topbar-inner">
      <a className="brand" href="#top" aria-label="Maze home">
        {/* Both logos are rendered; CSS shows the one for the active theme, so there is no flash and no script involved. */}
        <img className="brand-logo brand-logo-light" src={logoForLight} alt="Maze" width="99" height="48"/>
        <img className="brand-logo brand-logo-dark" src={logoForDark} alt="Maze" width="99" height="48"/>
      </a>
      <ThemeToggle/>
    </div></header>
    <main id="top">
      <section className="intro"><div className="intro-title"><h1>Find your way through the news.</h1></div><p className="intro-copy">Short audio briefings. Every figure traced to its source.</p></section>
      <TrailPath/>
      <div className="workspace">
        <aside className="setup-card" aria-label="Briefing preferences">
          <div className="card-heading"><h2>Choose your focus</h2></div>
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
                <select id="profile" value={profileId} onChange={event => {
                  setProfileId(event.target.value);
                  // A profile is heard in its own language unless the listener then chooses another.
                  const chosen = profiles.find(profile => profile.id === event.target.value)?.language;
                  if (chosen && languages.includes(chosen)) setLanguage(chosen);
                }}>
                  {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
                </select>
              </div>
              {activeProfile && <>
                {activeProfile.description && <p className="profile-description">{activeProfile.description}</p>}
                <div className="settings-row">
                  {activeProfile.duration && <span><Clock3 size={15}/>{activeProfile.duration} stories</span>}
                  {(language || activeProfile.language) && <span><AudioLines size={15}/>{languageLabel(language || activeProfile.language || 'en')}</span>}
                  {activeProfile.expertise && <span><Gauge size={15}/>{activeProfile.expertise.charAt(0).toUpperCase() + activeProfile.expertise.slice(1)}</span>}
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
                <label>Length<select value={duration} onChange={event => setDuration(Number(event.target.value))}><option value={3}>3 stories, 1 to 2 minutes</option><option value={5}>5 stories, 2 to 3 minutes</option><option value={8}>8 stories, 3 to 5 minutes</option><option value={12}>12 stories, 5 to 7 minutes</option><option value={16}>16 stories, 7 to 9 minutes</option></select></label>
              </div>
            </div>}
          <div className="delivery-settings">
            <div className="delivery-heading"><h3>Delivery</h3><p>Choose how your briefing should be delivered.</p></div>
            <label>Output language<select value={language} disabled={loadingListenerSettings} onChange={e => { setLanguage(e.target.value); updateScheduleDraft(v => ({ ...v, language: e.target.value })); }}>{languages.map(value => <option key={value} value={value}>{languageLabel(value)}</option>)}</select></label>
            {regions.length > 1 && <label>Region<select value={scheduleDraft.region} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, region: e.target.value }))}>{regions.map(value => <option key={value} value={value}>{regionLabel(value)}</option>)}</select></label>}
            {voicePresets.length > 1 && <div className="voice-field">
              <label>Voice<select value={scheduleDraft.voice_preset} disabled={loadingListenerSettings} aria-describedby="voice-note" onChange={e => { stopVoiceSample(); updateScheduleDraft(v => ({ ...v, voice_preset: e.target.value })); }}>{voicePresets.map(preset => <option key={preset.id} value={preset.id}>{voiceOptionLabel(preset, language)}</option>)}</select></label>
              {(() => {
                const chosen = voicePresets.find(preset => preset.id === scheduleDraft.voice_preset);
                if (!chosen || voiceVerifiedFor(chosen, language)) return null;
                const verified = voicePresets.filter(preset => preset.languages?.includes(language)).map(preset => preset.label);
                return <p className="microcopy voice-language-note">{chosen.label} is not verified for {englishLanguageName(language)}, so the accent may vary.{verified.length > 0 && ` Verified: ${verified.join(', ')}.`}</p>;
              })()}
              {voicePresets.find(preset => preset.id === scheduleDraft.voice_preset)?.preview && <button type="button" className="voice-sample" aria-pressed={sampleState !== 'idle'} onClick={toggleVoiceSample}>
                {sampleState === 'loading' ? <><LoaderCircle className="spin" size={14} aria-hidden="true"/> Loading sample</> : sampleState === 'playing' ? <><Square size={13} aria-hidden="true"/> Stop sample</> : <><Play size={13} aria-hidden="true"/> Play sample</>}
              </button>}
              {sampleError && <p className="action-error" role="alert">{sampleError}</p>}
            </div>}
            <p className="microcopy" id="voice-note">{voicePresets.length > 1 ? 'The voice applies to the next briefing you create. ' : ''}Change playback speed in the player at any time.</p>
          </div>
          {SHOW_SCHEDULE && <details className="schedule-settings">
            <summary><span><Clock3 size={15}/>Schedule delivery</span><ChevronDown size={15}/></summary>
            <div className="schedule-fields">
              <label>What to deliver<select value={scheduleDraft.wildcard ? CUSTOM_SCHEDULE : scheduleDraft.profile_id || profileId} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => e.target.value === CUSTOM_SCHEDULE ? { ...v, wildcard: v.wildcard ?? { topics: [], expertise: 'intermediate', duration: 5, language: v.language } } : { ...v, profile_id: e.target.value, wildcard: null })}>{profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}<option value={CUSTOM_SCHEDULE}>Custom briefing</option></select></label>
              {scheduleDraft.wildcard && <>
                <div className="schedule-topics"><span>Topics to follow</span>
                  <div className="topic-pills">
                    {topics.map(topic => {
                      const chosen = scheduleDraft.wildcard!.topics;
                      const selected = chosen.includes(topic);
                      return <button key={topic} type="button" className={selected ? 'topic-pill chosen' : 'topic-pill'}
                        aria-pressed={selected} disabled={loadingListenerSettings || (!selected && chosen.length >= MAX_TOPICS)}
                        onClick={() => updateScheduleDraft(v => v.wildcard ? { ...v, wildcard: { ...v.wildcard, topics: selected ? v.wildcard.topics.filter(value => value !== topic) : [...v.wildcard.topics, topic] } } : v)}>
                        {selected && <Check size={12} aria-hidden="true"/>}{topic}
                      </button>;
                    })}
                  </div>
                </div>
                <label>Depth<select value={scheduleDraft.wildcard.expertise} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => v.wildcard ? { ...v, wildcard: { ...v.wildcard, expertise: e.target.value } } : v)}><option value="beginner">Beginner</option><option value="intermediate">Intermediate</option><option value="advanced">Advanced</option></select></label>
                <label>Length<select value={scheduleDraft.wildcard.duration} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => v.wildcard ? { ...v, wildcard: { ...v.wildcard, duration: Number(e.target.value) } } : v)}><option value={3}>3 stories, 1 to 2 minutes</option><option value={5}>5 stories, 2 to 3 minutes</option><option value={8}>8 stories, 3 to 5 minutes</option><option value={12}>12 stories, 5 to 7 minutes</option><option value={16}>16 stories, 7 to 9 minutes</option></select></label>
                {scheduleDraft.wildcard.topics.length === 0 && <p className="schedule-note">Choose at least one topic to schedule a custom briefing.</p>}
              </>}
              <label>Cadence<select value={scheduleDraft.cadence} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, cadence: e.target.value as ScheduleDraft['cadence'] }))}><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label>
              {scheduleDraft.cadence === 'weekly' && <label>Weekday<select value={scheduleDraft.weekday ?? 0} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, weekday: Number(e.target.value) }))}>{['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'].map((day, index) => <option key={day} value={index}>{day}</option>)}</select></label>}
              <label>Local delivery time<input type="time" value={scheduleDraft.local_time} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, local_time: e.target.value }))}/></label>
              <label>Time zone (IANA)<input value={scheduleDraft.timezone} disabled={loadingListenerSettings} onChange={e => updateScheduleDraft(v => ({ ...v, timezone: e.target.value }))} placeholder="Europe/Amsterdam" autoComplete="off"/></label>
              <p className="schedule-note">Your briefing is prepared shortly before delivery and appears at the selected time.</p>
              <button className="retry-audio save-schedule" onClick={saveSchedule} disabled={scheduleBusy || preferencesLoading || loadingListenerSettings || !(scheduleDraft.wildcard ? scheduleDraft.wildcard.topics.length : scheduleDraft.profile_id)}>{scheduleBusy ? 'Saving schedule…' : schedule ? 'Save schedule changes' : 'Save delivery schedule'}</button>
              {scheduleMessage && <p className="schedule-message" role="status">{scheduleMessage}</p>}
              {schedule && <><p className={/fail|error|unavailable/i.test(schedule.dispatch_status) ? 'schedule-message schedule-failed' : 'schedule-message'} role="status">Scheduler status: {schedule.dispatch_status.replace(/_/g, ' ')}</p><p className="next-delivery">Next delivery {new Date(schedule.delivery_at).toLocaleString([], { timeZone: schedule.timezone, dateStyle: 'medium', timeStyle: 'short' })} ({schedule.timezone})</p></>}
            </div>
          </details>}
          <button className="primary-button" onClick={makeBriefing} disabled={Boolean(action) || preferencesLoading || !canGenerate}>
            {action === 'briefing' ? <><LoaderCircle className="spin" size={17}/> Preparing your briefing</> : 'Create my briefing'}
          </button>
          <p className={settingsChanged && action !== 'briefing' ? 'microcopy' : 'visually-hidden'} role="status">
            {action === 'briefing' ? 'Preparing your briefing.' : settingsChanged ? 'Settings changed. Create a new briefing to apply them.' : ''}
          </p>
          {briefingError && <p className="action-error" role="alert">{briefingError}</p>}
          <div className="privacy-note"><ShieldCheck size={15}/><span>No account. Your browser ID keeps your schedule and feedback together.</span></div>
        </aside>
        <section className="content-card" aria-label="Briefing and sources">
          <div className="content-tabs">
            <nav className="tabs-left" aria-label="Briefing views">
              <button aria-pressed={tab === 'listen'} className={tab === 'listen' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('listen')}><Headphones size={15}/> Listen</button>
              <button aria-pressed={tab === 'evidence'} className={tab === 'evidence' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('evidence')}><BookOpen size={15}/> Evidence</button>
              <button aria-pressed={tab === 'traces'} className={tab === 'traces' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('traces')}><Waypoints size={15}/> Traces</button>
              <button aria-pressed={tab === 'costs'} className={tab === 'costs' ? 'content-tab active' : 'content-tab'} onClick={() => changeView('costs')}><Info size={15}/> Costs</button>
            </nav>
          </div>
          {error && <div role="alert" className="error-banner"><CircleAlert size={16} aria-hidden="true"/><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={15}/></button></div>}
          <div hidden={tab !== 'listen'}>
            {history.length > 0 && <div className="briefing-history"><span className="section-kicker">Delivered briefings</span>{history.map(item => <button key={item.id} className={briefing?.id === item.id ? 'history-item selected' : 'history-item'} onClick={() => setBriefing(item)}><span>{item.profile_name}</span><span className="lang-tag">{item.language.toUpperCase()}</span><strong>{item.stories.length} {item.stories.length === 1 ? 'story' : 'stories'}</strong></button>)}</div>}
            {briefing ?
              <BriefingView briefing={briefing} headingRef={briefingHeading} playing={playing} audioRef={audio}
                onPlaybackChange={setPlaying} onAudioError={() => { setFailedPlayback(true); setPlaying(false); }}
                audioRetryTime={audioRetryTime} failedPlayback={failedPlayback} audioError={audioError} expanded={expanded} onExpand={setExpanded}
                onRetry={retryAudio} busy={Boolean(action)} retrying={action === 'audio'} evidence={evidence}
                onInspectEvidence={inspectEvidence} onTrace={inspectTrace}
                ratings={ratings} ratingBusy={ratingBusy} onRate={rateStory}/> :
              <div className="empty-state"><h2>No briefing yet</h2><p>Choose a profile or topics, then create your first audio briefing.</p></div>}
          </div>
          <div hidden={tab !== 'evidence'}>
            <EvidenceView evidence={evidence} facts={allFacts} loading={evidenceLoading} error={evidenceError}
              headingRef={evidenceHeading} storyId={evidenceStoryId}
              onBrowseAll={() => { focusEvidence.current = true; setEvidenceStoryId(null); }}
              onReturnToStory={() => { returnToStory.current = true; changeView('listen'); }}
              onRetry={() => { setEvidenceError(''); setEvidenceLoading(true); setEvidenceRequest(value => value + 1); }}/>
          </div>
          <div hidden={tab !== 'traces'}>
            <TracesView briefing={briefing} evidence={evidence} available={!briefing || availableNow(briefing)} availableAt={briefing?.available_at}
              loading={evidenceLoading} error={evidenceError} focus={traceFocus}
              onReturn={() => { returnToTrace.current = true; changeView('listen'); }}
              onRetry={() => { setEvidenceError(''); setEvidenceLoading(true); setEvidenceRequest(value => value + 1); }}/>
          </div>
          {tab === 'costs' && <CostsView costs={costs} loading={costsLoading}/>}
        </section>
      </div>
      <footer><span>Maze</span><span>Source links open original reporting.</span></footer>
    </main>
  </div>;
}

function BriefingView({ briefing, headingRef, playing, audioRef, onPlaybackChange, onAudioError, audioRetryTime, failedPlayback, audioError, expanded, onExpand, onRetry, busy, retrying, evidence, onInspectEvidence, onTrace, ratings, ratingBusy, onRate }: {
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
  onTrace: (segmentId: string, button: HTMLButtonElement) => void;
  ratings: Record<string, 'up' | 'down'>;
  ratingBusy: string | null;
  onRate: (storyId: string, rating: 'up' | 'down') => void;
}) {
  const available = availableNow(briefing);
  const audioSrc = briefing.audio_url && audioRetryTime
    ? `${briefing.audio_url}${briefing.audio_url.includes('?') ? '&' : '?'}retry=${audioRetryTime}`
    : briefing.audio_url;
  const ready = Boolean(audioSrc) && !failedPlayback && available;
  const usedSources = [...new Map(briefing.segments.flatMap(segment => segment.sources).map(source => [source.article_id, source])).values()];
  const unavailable = briefing.audio_status === 'unavailable';
  const storyCount = briefing.stories.length;
  const [speed, setSpeed] = useState(storedSpeed);
  useEffect(() => { applySpeed(audioRef.current, speed); writeSetting(SPEED_KEY, String(speed)); }, [speed, audioRef, audioSrc]);
  return <div className="briefing-view">
    <div className="briefing-meta">
      <span className="status-chip"><span className={ready ? 'live-dot' : 'status-dot'}/>{ready ? 'Ready to listen' : 'Transcript ready'}</span>
      <span className="briefing-for"><strong>{briefing.profile_name}</strong><span className="lang-tag">{languageLabel(briefing.language)}</span></span>
    </div>
    <div className="briefing-title-row">
      <h2 className="briefing-heading" ref={headingRef} tabIndex={-1}>Today, in context.</h2>
      <div className="briefing-facts" title={briefing.requested_minutes ? `You asked for ${briefing.requested_minutes} stories. Length depends on how many verified figures the reporting contains.` : undefined}>
        <span>{storyCount} {storyCount === 1 ? 'story' : 'stories'}</span>
        {/* estimated_seconds counts the figures alone; headlines, the opening line and pauses make the audio about three times as long (measured on rendered briefings). */}
        {typeof briefing.estimated_seconds === 'number' && <span><Clock3 size={14} aria-hidden="true"/>about {Math.max(1, Math.round(briefing.estimated_seconds * 3 / 60))} min</span>}
      </div>
    </div>
    <div className={ready ? 'player ready' : 'player'}>
      <div className="player-info">
        <strong>{ready ? (playing ? 'Now playing' : 'Listen to your briefing') : available ? 'Read your briefing below' : 'Your briefing is scheduled'}</strong>
        <span>{ready ? 'AI-generated narration' : !available ? `Audio will be available ${new Date(briefing.available_at!).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}.` : failedPlayback ? 'The audio file could not be played.' : unavailable ? 'Audio generation is unavailable.' : 'Audio is ready to generate.'}</span>
      </div>
      {ready ?
        <div className="player-controls">
          <audio key={audioSrc} ref={audioRef} className="audio-controls" controls preload="metadata"
            src={audioSrc ?? undefined} aria-label={`${briefing.profile_name} audio briefing`}
            onLoadedMetadata={event => applySpeed(event.currentTarget, speed)}
            onPlay={() => onPlaybackChange(true)} onPause={() => onPlaybackChange(false)}
            onEnded={() => onPlaybackChange(false)} onError={onAudioError}/>
          <div className="speed-control" role="group" aria-label="Playback speed">
            {PLAYBACK_SPEEDS.map(value => <button key={value} type="button" aria-pressed={speed === value}
              className={speed === value ? 'speed-option active' : 'speed-option'} onClick={() => setSpeed(value)}>{value}×</button>)}
          </div>
        </div> :
        available && <button className="retry-audio" onClick={onRetry} disabled={busy}>
          <RotateCw size={14}/>{retrying ? 'Generating audio…' : failedPlayback ? 'Reload audio' : unavailable ? 'Retry audio' : 'Generate audio'}
        </button>}
    </div>
    {(audioError || failedPlayback) && <p className="action-error" role="alert">{audioError || 'Playback failed. Reload the audio or keep reading.'}</p>}
    {ready && <details className="briefing-fold briefing-sources" aria-label="Sources used by this briefing"><summary><span><BookOpen size={15}/>Sources used<em>{usedSources.length}</em></span><ChevronDown size={15}/></summary><div className="briefing-fold-body"><p className="fold-note">Reporting cited in this briefing.</p>{usedSources.length ? <div className="briefing-source-list">{usedSources.map(source => <a key={source.article_id} href={source.url} target="_blank" rel="noreferrer"><span>{source.source}</span><strong>{source.title}</strong></a>)}</div> : <p className="no-sources">No source links were attached to this briefing.</p>}</div></details>}
    {briefing.stories.length > 0 && <details className="briefing-fold briefing-review"><summary><span><ThumbsUp size={15}/>Review this briefing</span><ChevronDown size={15}/></summary><div className="briefing-fold-body"><p className="fold-note">Rate the stories you heard. Your next briefings lean towards the topics of stories you mark helpful and away from the topics of those you mark not useful.</p>{briefing.stories.map(story => <div className="story-feedback" key={story.id} aria-label={`Rate ${story.title}`}><span>{story.title}</span><button type="button" className={ratings[story.id] === 'up' ? 'feedback-button chosen' : 'feedback-button'} onClick={() => onRate(story.id, 'up')} disabled={busy || ratingBusy === story.id} aria-label={`Mark ${story.title} useful`} aria-pressed={ratings[story.id] === 'up'}><ThumbsUp size={14}/> Helpful</button><button type="button" className={ratings[story.id] === 'down' ? 'feedback-button chosen' : 'feedback-button'} onClick={() => onRate(story.id, 'down')} disabled={busy || ratingBusy === story.id} aria-label={`Mark ${story.title} not useful`} aria-pressed={ratings[story.id] === 'down'}><ThumbsDown size={14}/> Not useful</button></div>)}</div></details>}
    <div className="story-list">
      <div className="section-kicker"><span>In this briefing</span></div>
      {briefing.stories.map(story => {
        const storyEvidence = evidence.find(item => item.id === story.id);
        const segments = briefing.segments.filter(segment =>
          segment.story_id === story.id || segment.id === 'segment-' + story.id ||
          storyEvidence?.facts.some(fact => segment.fact_ids.includes(fact.id)));
        return <article className="story" key={story.id}>
          <div className="story-body">
            <h3>{story.title}</h3>
            {story.source_title && <p className="translated-headline">Translated headline. Original: {story.source_title}</p>}
            <p>{story.reason}</p>
            <div className="transcript">
              {segments.map(segment => <div className="transcript-segment" key={segment.id}>
                {segment.refresher_text && <p className="transcript-refresher">{segment.refresher_text}</p>}
                <p>{segment.text}{segment.conflict && <span className="conflict-flag"><Info size={12}/> Conflicting reports</span>}</p>
                {available && <div className="segment-actions">
                  <button className="source-toggle" aria-expanded={expanded === segment.id} onClick={() => onExpand(expanded === segment.id ? null : segment.id)}>
                    <BookOpen size={13}/>{segment.sources.length} {segment.sources.length === 1 ? 'source' : 'sources'}
                    <ChevronDown size={13} className={expanded === segment.id ? 'rotated' : ''}/>
                  </button>
                  <button className="source-toggle trace-link" aria-label={`Trace the claims in this segment of ${story.title} to their sources`} onClick={event => onTrace(segment.id, event.currentTarget)}>
                    <Waypoints size={13}/> Trace claims
                  </button>
                </div>}
                {available && expanded === segment.id && <div className="source-list">
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
    <div className="voice-disclosure"><p><strong>About this audio</strong><br/>The narration is an AI-generated voice. Story selection and summaries are grounded in the cited reporting. This briefing reports figures; it is not investment advice. Listen critically, especially where sources disagree.</p></div>
  </div>;
}

type EvidenceEntry =
  | { kind: 'story'; story: Evidence; weight: number; date: string }
  | { kind: 'event'; thread: EventThread<Evidence>; shown: Evidence[]; weight: number; date: string };
const storyMatches = (story: Evidence, term: string) => !term || [story.title, ...story.articles.map(article => `${article.title} ${article.source}`)].some(value => value.toLowerCase().includes(term));

// Merge badges: translated editions of one outlet, and distinct outlets, that were combined into this story.
function MergeBadges({ merge }: { merge: Merge }) {
  return <>
    {merge.editions > 1 && <span className="merge-badge">
      <Languages size={12} aria-hidden="true"/>{merge.editions} editions
      <span className="merge-langs" aria-hidden="true">{merge.editionLanguages.map(code => code.toUpperCase()).join(' · ')}</span>
      <span className="visually-hidden"> in {joinList(merge.editionLanguages.map(englishLanguageName))}</span>
    </span>}
    {merge.outlets > 1 && <span className="merge-badge"><Newspaper size={12} aria-hidden="true"/>{merge.outlets} outlets</span>}
  </>;
}
function MergeNote({ merge }: { merge: Merge }) {
  if (merge.articles < 2) return null;
  const listed = merge.families.slice(0, 4).map(family => family.languages.length > 1 ? `${family.name} (${family.languages.map(englishLanguageName).join(', ')})` : family.name);
  const more = merge.families.length - listed.length;
  return <p className="merge-note">Merged from {merge.articles} articles: {listed.join('; ')}{more > 0 ? ` and ${more} more` : ''}.</p>;
}

function EvidenceCard({ item, expanded, single = false, dated = false, onToggle }: { item: Evidence; expanded: boolean; single?: boolean; dated?: boolean; onToggle?: () => void }) {
  const merge = mergeInfo(item);
  const date = dated ? shortDate(item.publication_date) : '';
  return <article className="evidence-card">
    {single ?
      <>
        <h3 className="evidence-title"><strong>{item.title}</strong></h3>
        {hasMergeBadges(merge) && <div className="evidence-meta evidence-meta-single"><MergeBadges merge={merge}/></div>}
      </> :
      <button id={`evidence-story-${item.id}`} className="evidence-title" onClick={onToggle} aria-expanded={expanded}>
        <span>
          <span className="evidence-meta"><span className="evidence-count">{date && `${date} · `}{count(item.articles.length, 'source')}</span><MergeBadges merge={merge}/></span>
          <strong>{item.title}</strong>
        </span>
        <ChevronDown size={17} className={expanded ? 'rotated' : ''}/>
      </button>}
    {expanded && <div className="evidence-detail">
      <MergeNote merge={merge}/>
      <div className="evidence-sources">
        {item.articles.map(article => <a key={article.article_id} href={article.url} target="_blank" rel="noreferrer">
          <span>{article.source}{article.language && <small className="lang-tag">{article.language.toUpperCase()}<span className="visually-hidden"> ({englishLanguageName(article.language)})</span></small>}</span>
          <strong>{article.title}</strong><span>Open source ↗</span>
        </a>)}
      </div>
      {item.facts.length > 0 && <div className="fact-list">
        <span className="section-kicker">Extracted claims</span>
        {item.facts.map(fact => <div className="fact" key={fact.id}>
          <span className={['conflict', 'unresolved', 'needs_review'].includes(fact.conflict_status) ? 'fact-dot conflict' : 'fact-dot'}/>
          <div><p>{fact.statement}</p>{(fact.value || fact.unit) && <span>{fact.value} {fact.unit}</span>}{fact.evidence && <blockquote>“{fact.evidence}”</blockquote>}</div>
        </div>)}
      </div>}
    </div>}
  </article>;
}

// One developing event: stories linked by the backend, shown under a single collapsible header.
function EventGroup({ entry, expanded, openStory, onToggle, onToggleStory }: {
  entry: Extract<EvidenceEntry, { kind: 'event' }>;
  expanded: boolean;
  openStory: string | null;
  onToggle: () => void;
  onToggleStory: (storyId: string) => void;
}) {
  const { thread, shown } = entry;
  const panel = `event-${thread.id}`;
  return <article className="evidence-card event-card">
    <button id={`event-toggle-${thread.id}`} className="event-toggle" onClick={onToggle} aria-expanded={expanded} aria-controls={expanded ? panel : undefined}>
      <span>
        <span className="event-kicker"><Layers size={14} aria-hidden="true"/>{eventSummary(threadStats(thread))}{shown.length < thread.stories.length && <span className="event-match"> · {shown.length} of {thread.stories.length} match</span>}</span>
        <strong>{thread.title}</strong>
      </span>
      <ChevronDown size={17} className={expanded ? 'rotated' : ''}/>
    </button>
    {expanded && <div className="event-members" id={panel}>
      {shown.map(story => <EvidenceCard key={story.id} item={story} dated expanded={openStory === story.id} onToggle={() => onToggleStory(story.id)}/>)}
    </div>}
  </article>;
}

function Stat({ value, one, many }: { value: number; one: string; many: string }) {
  return <span><strong>{value.toLocaleString('en')}</strong> {value === 1 ? one : many}</span>;
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
  // Explicit expand/collapse choices per event; until one is made, events are collapsed, or open while searching.
  const [eventOpen, setEventOpen] = useState<Record<string, boolean>>({});
  const [reveal, setReveal] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(20);
  const term = query.trim().toLowerCase();
  const currentStory = evidence.find(item => item.id === storyId);
  const threads = useMemo(() => eventThreads(evidence), [evidence]);
  const currentThread = currentStory?.thread_id ? threads.get(currentStory.thread_id) : undefined;
  const totals = useMemo(() => ({
    articles: evidence.reduce((sum, story) => sum + story.articles.length, 0),
    linked: [...threads.values()].reduce((sum, thread) => sum + thread.stories.length, 0),
  }), [evidence, threads]);
  // Events come first (most reports, then most articles), then single stories (most articles). A search keeps an event if any member matches.
  const entries = useMemo(() => {
    const events: EvidenceEntry[] = [];
    const singles: EvidenceEntry[] = [];
    for (const thread of threads.values()) {
      const shown = thread.stories.filter(story => storyMatches(story, term));
      if (shown.length) events.push({ kind: 'event', thread, shown, weight: thread.stories.length * 10_000 + thread.stories.reduce((sum, story) => sum + story.articles.length, 0), date: thread.stories[thread.stories.length - 1].publication_date ?? '' });
    }
    for (const story of evidence) {
      if (story.thread_id && threads.has(story.thread_id)) continue;
      if (storyMatches(story, term)) singles.push({ kind: 'story', story, weight: story.articles.length, date: story.publication_date ?? '' });
    }
    const byWeight = (left: EvidenceEntry, right: EvidenceEntry) => right.weight - left.weight || right.date.localeCompare(left.date);
    return [...events.sort(byWeight), ...singles.sort(byWeight)];
  }, [evidence, threads, term]);
  const visible = entries.slice(0, limit);
  const eventExpanded = (id: string) => eventOpen[id] ?? term !== '';

  // "Browse all evidence" from one story lands back on that story, with its event expanded.
  function browseAll() {
    if (currentStory) {
      if (term && !storyMatches(currentStory, term)) setQuery('');
      if (currentThread) setEventOpen(value => ({ ...value, [currentThread.id]: true }));
      setOpen(currentStory.id);
      setReveal(currentStory.id);
    }
    onBrowseAll();
  }
  useEffect(() => {
    if (!reveal || currentStory) return;
    const index = entries.findIndex(entry => entry.kind === 'story' ? entry.story.id === reveal : entry.shown.some(story => story.id === reveal));
    if (index < 0) { setReveal(null); return; }
    if (index >= limit) { setLimit(index + 1); return; }
    // The parent focuses the heading in this same commit; move focus on to the story afterwards.
    const timer = window.setTimeout(() => { document.getElementById(`evidence-story-${reveal}`)?.focus(); setReveal(null); }, 0);
    return () => window.clearTimeout(timer);
  }, [reveal, currentStory, entries, limit]);

  const mergedNote = [
    totals.articles > evidence.length && `${count(totals.articles, 'article')} merged into ${count(evidence.length, 'story', 'stories')}.`,
    threads.size > 0 && `${count(totals.linked, 'story', 'stories')} grouped into ${count(threads.size, 'event')}.`,
  ].filter(Boolean).join(' ');

  return <div className="data-view">
    <div className="data-heading">
      <h2 className="briefing-heading" ref={headingRef} tabIndex={-1}>{currentStory ? 'Evidence for this story' : 'Evidence, in the open.'}</h2>
      <p>Every briefing is built from reporting and extracted claims. Follow the links back to their sources.</p>
    </div>
    {loading ? <LoadingRow text="Loading evidence…"/> : error ?
      <div>
        <p className="action-error" role="alert">{error}</p>
        <button className="retry-audio" onClick={onRetry}><RotateCw size={14}/> Retry loading evidence</button>
      </div> : evidence.length === 0 ? <EmptyRow text="There are no prepared stories to inspect yet."/> : <>
        {currentStory ?
          <>
            <div className="evidence-actions">
              <button className="source-toggle" onClick={onReturnToStory}>Back to briefing story</button>
              <button className="source-toggle" onClick={browseAll}>Browse all evidence</button>
            </div>
            {currentThread && <p className="event-context"><Layers size={14} aria-hidden="true"/><span><span>{eventSummary(threadStats(currentThread), 'Part of an event')}</span><strong>{currentThread.title}</strong></span></p>}
          </> :
          <>
            <div className="evidence-summary">
              <Stat value={evidence.length} one="story" many="stories"/>
              {threads.size > 0 && <Stat value={threads.size} one="event" many="events"/>}
              <Stat value={totals.articles} one="article" many="articles"/>
              <Stat value={facts.length} one="extracted claim" many="extracted claims"/>
            </div>
            {mergedNote && <p className="evidence-note">{mergedNote}</p>}
            {evidence.length > 1 && <label className="evidence-search">Find a story or publisher
              <input type="search" value={query} onChange={event => { setQuery(event.target.value); setLimit(20); }}/>
            </label>}
            {entries.length === 0 && <EmptyRow text="No stories match your search."/>}
          </>}
        {currentStory ?
          <EvidenceCard item={currentStory} single expanded/> :
          visible.map(entry => entry.kind === 'story' ?
            <EvidenceCard key={entry.story.id} item={entry.story} expanded={open === entry.story.id} onToggle={() => setOpen(open === entry.story.id ? null : entry.story.id)}/> :
            <EventGroup key={entry.thread.id} entry={entry} expanded={eventExpanded(entry.thread.id)} openStory={open}
              onToggle={() => setEventOpen(value => ({ ...value, [entry.thread.id]: !eventExpanded(entry.thread.id) }))}
              onToggleStory={id => setOpen(open === id ? null : id)}/>)}
        {!currentStory && entries.length > limit && <button className="retry-audio more-evidence" onClick={() => setLimit(value => value + 20)}>
          Show more ({limit} of {entries.length.toLocaleString('en')})
        </button>}
      </>}
  </div>;
}
function CostsView({ costs, loading }: { costs: Costs | null; loading: boolean }) {
  return <div className="data-view"><div className="data-heading"><h2>What does it cost?</h2><p>Provider usage and estimated costs for preparing stories and producing briefings.</p></div>{loading ? <LoadingRow/> : !costs ? <EmptyRow text="Cost estimates are not available right now."/> : <><div className="cost-hero"><span>Estimated cost per briefing so far</span><strong>{money(costs.per_briefing_usd)}</strong><span>average on this server; it falls as listeners share rendered stories</span></div><div className="cost-table-wrap"><table className="cost-table"><thead><tr><th>Stage</th><th>Calls</th><th>Tokens / chars</th><th>Est. cost</th></tr></thead><tbody>{costs.stages.map((s, i) => <tr key={`${s.stage}-${i}`}><td>{s.stage}</td><td>{s.calls}</td><td>{s.input_tokens + s.output_tokens > 0 ? `${(s.input_tokens + s.output_tokens).toLocaleString()} tokens` : `${s.characters.toLocaleString()} chars`}</td><td>{money(s.estimated_usd)}</td></tr>)}</tbody><tfoot><tr><td colSpan={3}>Per briefing total</td><td>{money(costs.per_briefing_usd)}</td></tr></tfoot></table></div><div className="cost-notes"><div><span>Preparation</span><strong>{money(costs.preparation_usd)}</strong><p>Preparation across {costs.article_count.toLocaleString()} articles.</p></div><div><span>At 10x article volume</span><strong>{money(costs.ten_x_preparation_usd)}</strong><p>Projected preparation cost at ten times the article volume.</p></div></div><div className="cost-footnote"><Info size={14}/><span>Each story is rendered once per voice and language and reused by every listener who gets it, so the first briefings carry the cost and later ones add little. Speech calls count newly synthesized clips. Mode: {costs.mode}.</span></div></>}</div>;
}
// The signature: one thin winding line, drawn once on load, that leads from the headline into the briefing.
// It uses the same 2px line and arrowhead as the connectors in Linked Traces.
function TrailPath() {
  return <svg className="hero-path" viewBox="0 0 420 72" aria-hidden="true" focusable="false">
    <path className="hero-path-line" pathLength={1} d="M2 8 C 60 8 70 44 128 44 C 186 44 190 12 248 12 C 306 12 330 40 398 40 L 398 59"/>
    <polygon className="hero-path-head" points="389,58 407,58 398,71"/>
  </svg>;
}
function LoadingRow({ text = 'Loading data…' }: { text?: string }) { return <div className="data-loading" role="status"><LoaderCircle className="spin" size={18} aria-hidden="true"/> {text}</div>; }
function EmptyRow({ text }: { text: string }) { return <div className="data-loading" role="status">{text}</div>; }
