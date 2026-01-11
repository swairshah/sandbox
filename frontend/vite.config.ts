import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/chat': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/api': 'http://localhost:8000',
      '/ws/chat': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      '/ws/files': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    }
  }
})
