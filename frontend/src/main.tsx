import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

// No web font: the terminal-board theme (index.css) uses the system monospace stack for
// everything, so the old Press Start 2P display face is no longer imported.

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
