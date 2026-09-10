import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// The latin subset only (S0-fe-2). The package also ships cyrillic/greek/latin-ext; the
// app is `lang="en"` and the display font is used for chrome, never for device data, so
// the other subsets would be ~30 KB of woff2 nothing ever renders.
import '@fontsource/press-start-2p/latin-400.css'

import App from './App'
import './index.css'

const root = document.getElementById('root')
if (!root) {
  throw new Error('#root is missing from index.html')
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
