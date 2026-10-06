import { useThemeContext } from '../context/ThemeContext'
export type { Theme } from '../lib/theme'

/** Shared theme state: `{ theme, setTheme, toggleTheme }` from <ThemeProvider>. */
export function useTheme() {
  return useThemeContext()
}
