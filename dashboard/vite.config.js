import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `npm run dev` serves on :5173 and forwards API + WebSocket calls to the backend on :8000.
// `npm run build` writes dist/, which the backend serves at http://127.0.0.1:8000.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
