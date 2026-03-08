import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/auth': 'http://127.0.0.1:8765',
      '/chats': 'http://127.0.0.1:8765',
      '/compose': 'http://127.0.0.1:8765',
      '/todos': 'http://127.0.0.1:8765',
      '/contacts': 'http://127.0.0.1:8765',
      '/organizations': 'http://127.0.0.1:8765',
      '/scan': 'http://127.0.0.1:8765',
      '/settings': 'http://127.0.0.1:8765',
      '/ws': {
        target: 'ws://127.0.0.1:8765',
        ws: true,
      },
    },
  },
})
