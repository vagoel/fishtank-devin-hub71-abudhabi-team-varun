import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { gptLiveSessionPlugin } from './dev/gptLiveSession.js'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), gptLiveSessionPlugin()],
  resolve: { dedupe: ['react', 'react-dom'] },
  test: {
    include: ['src/**/*.test.{js,jsx}', 'dev/**/*.test.js'],
    environment: 'jsdom',
    setupFiles: ['./src/testSetup.js'],
    restoreMocks: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
    },
  },
})
