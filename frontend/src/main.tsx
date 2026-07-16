// Bootstrap — mount <App /> and pull in the Tailwind stylesheet.
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles/index.css'
import { ThemeProvider, readStoredTheme } from './contexts/ThemeContext'

// Apply the stored theme BEFORE first paint so there's no flash on load.
if (readStoredTheme() === 'dark') {
  document.documentElement.classList.add('dark')
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </React.StrictMode>,
)
