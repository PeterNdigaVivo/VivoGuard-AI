import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'

// Vite config — keep it simple. The dev server proxies /api and /ws to
// the backend so we don't have to fight CORS during development.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true, rewrite: p => p.replace(/^\/api/, '') },
      '/ws':  { target: 'ws://localhost:8000',   ws: true, changeOrigin: true },
    },
  },
})
