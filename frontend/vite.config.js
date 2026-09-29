/**
 * frontend/vite.config.js — Vite + Vue 3 + vite-plugin-pwa Configuration.
 *
 * Configures:
 * 1. Installable Standalone Web App Manifest for iOS Safari & Android Chrome
 *    (`short_name: 'ChronoMed'` <= 12 chars, separate `any` and `maskable` icons).
 * 2. Workbox Service Worker Runtime Caching:
 *    - `NetworkFirst` (with 3-second fallback timeout) for `/api/v1/shifts`,
 *      `/api/v1/requests`, and `/api/v1/timelogs` so physicians can view their cached
 *      schedule even inside hospital surgical suites or basement dead zones.
 *    - `CacheFirst` for Google Fonts & static iconography.
 */

import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import tailwindcss from '@tailwindcss/vite';
import { VitePWA } from 'vite-plugin-pwa';

export default defineConfig({
  plugins: [
    vue(),
    tailwindcss(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.ico', 'apple-touch-icon.png', 'icon.svg'],
      manifest: {
        id: '/',
        name: 'ChronoMed ICU & OR Physician Companion',
        short_name: 'ChronoMed',
        description:
          'Mobile PWA for Anesthesiology & ICU shift schedules, 32h fatigue alerts, time tracking, and peer shift swaps.',
        theme_color: '#020617',
        background_color: '#020617',
        display: 'standalone',
        orientation: 'portrait',
        start_url: '/',
        scope: '/',
        icons: [
          {
            src: '/pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png',
            purpose: 'any',
          },
          {
            src: '/pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'any',
          },
          {
            src: '/pwa-maskable-512x512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,ico,png,svg,woff2}'],
        runtimeCaching: [
          {
            // Cache Schedule, Swap Requests & TimeLogs API for Hospital Dead-Zone Offline Access
            urlPattern: ({ url }) =>
              url.pathname.startsWith('/api/v1/shifts') ||
              url.pathname.startsWith('/api/v1/requests') ||
              url.pathname.startsWith('/api/v1/timelogs'),
            handler: 'NetworkFirst',
            options: {
              cacheName: 'chronomed-schedule-api-cache',
              networkTimeoutSeconds: 3, // Fall back to cached schedule in 3s if in dead zone
              expiration: {
                maxEntries: 60,
                maxAgeSeconds: 60 * 60 * 24 * 7, // Keep 7 days of offline schedule data
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
          {
            // StaleWhileRevalidate for Hospital & Analytics metadata
            urlPattern: ({ url }) => url.pathname.startsWith('/api/v1/analytics'),
            handler: 'StaleWhileRevalidate',
            options: {
              cacheName: 'chronomed-analytics-cache',
              expiration: {
                maxEntries: 30,
                maxAgeSeconds: 60 * 60 * 24 * 3,
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
          {
            urlPattern: /^https:\/\/fonts\.(?:googleapis|gstatic)\.com\/.*/i,
            handler: 'CacheFirst',
            options: {
              cacheName: 'chronomed-google-fonts',
              expiration: {
                maxEntries: 10,
                maxAgeSeconds: 60 * 60 * 24 * 365,
              },
              cacheableResponse: {
                statuses: [0, 200],
              },
            },
          },
        ],
      },
      devOptions: {
        enabled: true,
        type: 'module',
      },
    }),
  ],
});
