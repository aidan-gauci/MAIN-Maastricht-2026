import { useEffect, useState } from 'react';
import { Monitor, Moon, Sun } from 'lucide-react';

/*
 * Colour theme: System (follow the OS), Light or Dark.
 *
 * The choice is stored in localStorage under 'maze-theme' and applied as data-theme on <html>.
 * index.html sets the attribute before first paint, so this module only has to keep it in sync
 * afterwards. Storage can be blocked (private windows); every access is wrapped, and the toggle
 * still works for the current page when saving fails.
 */

export type ThemePreference = 'system' | 'light' | 'dark';
export const THEME_KEY = 'maze-theme';

const OPTIONS = [
  { id: 'system', label: 'System', Icon: Monitor },
  { id: 'light', label: 'Light', Icon: Sun },
  { id: 'dark', label: 'Dark', Icon: Moon },
] as const;

// Page background of each theme, for the browser's own chrome (mobile address bar). Matches --bg in styles.css.
const BROWSER_COLOR = { light: '#f5f3ec', dark: '#121d18' } as const;

const isPreference = (value: unknown): value is ThemePreference => value === 'system' || value === 'light' || value === 'dark';

export function readThemePreference(): ThemePreference {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return isPreference(value) ? value : 'system';
  } catch {
    return 'system';
  }
}

function applyTheme(preference: ThemePreference) {
  document.documentElement.setAttribute('data-theme', preference);
  // index.html declares one theme-color per scheme. Forcing a theme must override the OS-matched one.
  document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(meta => {
    const own = meta.media.includes('dark') ? 'dark' : 'light';
    meta.content = BROWSER_COLOR[preference === 'system' ? own : preference];
  });
}

export function ThemeToggle() {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference);

  useEffect(() => { applyTheme(preference); }, [preference]);

  // Keep several open tabs in step.
  useEffect(() => {
    const sync = (event: StorageEvent) => { if (event.key === null || event.key === THEME_KEY) setPreference(readThemePreference()); };
    window.addEventListener('storage', sync);
    return () => window.removeEventListener('storage', sync);
  }, []);

  function choose(next: ThemePreference) {
    setPreference(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* the choice still applies to this page */ }
  }

  return <div className="theme-toggle" role="group" aria-label="Color theme">
    {OPTIONS.map(({ id, label, Icon }) =>
      <button key={id} type="button" aria-pressed={preference === id} onClick={() => choose(id)}>
        <Icon size={15} aria-hidden="true"/><span className="theme-label">{label}</span>
      </button>)}
  </div>;
}
