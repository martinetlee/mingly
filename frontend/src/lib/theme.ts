/**
 * Theme persistence + application. index.html runs an inline copy of
 * readStoredTheme/applyTheme before first paint (keep the key in sync).
 */
export type Theme = 'dark' | 'light'

export const THEME_STORAGE_KEY = 'mingly_theme'

export function readStoredTheme(): Theme {
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch {
    // storage unavailable
  }
  return 'dark' // default
}

export function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // storage unavailable — theme still applies for this session
  }
}

export function applyTheme(theme: Theme): void {
  if (theme === 'light') document.documentElement.setAttribute('data-theme', 'light')
  else document.documentElement.removeAttribute('data-theme')
}
