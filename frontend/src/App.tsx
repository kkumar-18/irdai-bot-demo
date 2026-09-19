import { useEffect, useState } from 'react'
import './App.css'
import { checkHealth } from './api'
import { ChatWindow } from './components/ChatWindow'

function App() {
  const [apiUp, setApiUp] = useState<boolean | null>(null)

  useEffect(() => {
    checkHealth().then(setApiUp)
  }, [])

  return (
    <div className="app-shell">
      <header className="app-header">
        <h1>HDFC Life vs Axis Max Life</h1>
        <p className="app-subtitle">Public disclosure comparisons and guaranteed-plan quote comparisons</p>
        {apiUp === false && (
          <p className="api-warning">
            Can't reach the backend at <code>/api</code>. Start it with{' '}
            <code>uv run uvicorn irdai_bot.api:app --port 8000</code>.
          </p>
        )}
      </header>
      <main className="app-main">
        <ChatWindow />
      </main>
    </div>
  )
}

export default App
