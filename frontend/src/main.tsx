import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App'
import { applyTheme, readStoredTheme } from './lib/theme'

// index.html already applied the theme before first paint; re-apply in case
// the inline script was stripped (e.g. a strict CSP)
applyTheme(readStoredTheme())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
