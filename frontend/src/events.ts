// How the Evidence view shows that the backend made sense of the news: editions and duplicate reports merged
// into one story, and stories about one developing event linked into an event thread. Pure functions, no React.

export type EvidenceArticle = { source: string; language?: string };
export type EvidenceStory = { id: string; title: string; publication_date?: string; articles: EvidenceArticle[]; thread_id?: string; thread_title?: string };

// Translated editions are listed as "Outlet - German" or "Outlet - Dutch"; they belong to the family "Outlet".
const EDITION_SUFFIX = /\s+[-–—]\s+(German|Dutch|English|Deutsch|Nederlands)\s*$/i;
const SUFFIX_LANGUAGE: Record<string, string> = { german: 'de', deutsch: 'de', dutch: 'nl', nederlands: 'nl', english: 'en' };

export const outletFamily = (source: string) => source.replace(EDITION_SUFFIX, '').replace(/\s+/g, ' ').trim() || source.trim();
const articleLanguage = (article: EvidenceArticle) => {
  const code = article.language?.trim().toLowerCase().split(/[-_]/)[0];
  if (code) return code;
  return SUFFIX_LANGUAGE[article.source.match(EDITION_SUFFIX)?.[1].toLowerCase() ?? ''];
};
const byLanguage = (left: string, right: string) => (left === 'en' ? -1 : right === 'en' ? 1 : left.localeCompare(right));

export type OutletFamily = { name: string; languages: string[]; articles: number };
export const outletFamilies = (articles: EvidenceArticle[]): OutletFamily[] => {
  const families = new Map<string, OutletFamily>();
  for (const article of articles) {
    const name = outletFamily(article.source);
    const key = name.toLocaleLowerCase();
    const family = families.get(key) ?? { name, languages: [], articles: 0 };
    const language = articleLanguage(article);
    if (language && !family.languages.includes(language)) family.languages = [...family.languages, language].sort(byLanguage);
    family.articles += 1;
    families.set(key, family);
  }
  return [...families.values()];
};

export type Merge = { families: OutletFamily[]; editions: number; editionLanguages: string[]; outlets: number; articles: number };
// "Editions": one outlet family reporting in several languages. "Outlets": distinct outlet families.
export const mergeInfo = (story: EvidenceStory): Merge => {
  const families = outletFamilies(story.articles);
  const translated = families.filter(family => family.languages.length > 1);
  return {
    families,
    editions: translated.reduce((total, family) => total + family.languages.length, 0),
    editionLanguages: [...new Set(translated.flatMap(family => family.languages))].sort(byLanguage),
    outlets: families.length,
    articles: story.articles.length,
  };
};
export const hasMergeBadges = (merge: Merge) => merge.editions > 1 || merge.outlets > 1;

export type EventThread<T extends EvidenceStory = EvidenceStory> = { id: string; title: string; stories: T[] };
// Only threads of two or more stories count; members are in the order the event unfolded.
export const eventThreads = <T extends EvidenceStory>(stories: T[]): Map<string, EventThread<T>> => {
  const grouped = new Map<string, T[]>();
  for (const story of stories) {
    if (!story.thread_id) continue;
    grouped.set(story.thread_id, [...(grouped.get(story.thread_id) ?? []), story]);
  }
  const threads = new Map<string, EventThread<T>>();
  for (const [id, members] of grouped) {
    if (members.length < 2) continue;
    const ordered = [...members].sort((left, right) => (left.publication_date ?? '').localeCompare(right.publication_date ?? '') || left.id.localeCompare(right.id));
    threads.set(id, { id, title: members.find(story => story.thread_title)?.thread_title || ordered[0].title, stories: ordered });
  }
  return threads;
};

const DAY = 86_400_000;
const dayOf = (value?: string) => (value ? Date.parse(value.slice(0, 10)) : NaN);
export type ThreadStats = { reports: number; days: number | null; outlets: number; articles: number };
export const threadStats = (thread: EventThread): ThreadStats => {
  const days = thread.stories.map(story => dayOf(story.publication_date)).filter(day => !Number.isNaN(day));
  return {
    reports: thread.stories.length,
    // First report to last report: stories on 13 and 18 November span 5 days; the same date is 0.
    days: days.length ? Math.round((Math.max(...days) - Math.min(...days)) / DAY) : null,
    outlets: outletFamilies(thread.stories.flatMap(story => story.articles)).length,
    articles: thread.stories.reduce((total, story) => total + story.articles.length, 0),
  };
};

export const count = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en')} ${n === 1 ? one : many}`;
// "Event · 17 reports over 5 days · 2 outlets"
export const eventSummary = (stats: ThreadStats, label = 'Event') => [
  label,
  stats.days === null ? count(stats.reports, 'report') : `${count(stats.reports, 'report')} ${stats.days === 0 ? 'on a single day' : `over ${count(stats.days, 'day')}`}`,
  count(stats.outlets, 'outlet'),
].join(' · ');

export const shortDate = (value?: string) => {
  const day = dayOf(value);
  return Number.isNaN(day) ? '' : new Date(day).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
};
export const joinList = (items: string[]) => items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
