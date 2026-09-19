import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Avoids CORS in dev — the FastAPI CORS config (src/irdai_bot/api.py)
      // is the fallback for any non-proxied access.
      '/api': 'http://localhost:8000',
    },
  },
})
