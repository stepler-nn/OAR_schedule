/**
 * services/api.js — Central Axios HTTP Client for ChronoMed API.
 *
 * Configured with:
 * 1. Base URL: `/api/v1`
 * 2. Request Interceptor: Automatically injects JWT Bearer token from localStorage.
 * 3. Response Interceptor: Intercepts HTTP 401 Unauthorized errors, clears stale
 *    credentials, and redirects to `/login` (preventing redirect loops).
 */

import axios from 'axios';

export const TOKEN_STORAGE_KEY = 'token';
export const LEGACY_STORAGE_KEY = 'chronomed_jwt';

/**
 * Retrieves the current JWT authentication token from persistent browser storage.
 * Supports standard 'token' key with fallback to legacy 'chronomed_jwt'.
 */
export function getStoredToken() {
  if (typeof window === 'undefined') return null;
  return (
    localStorage.getItem(TOKEN_STORAGE_KEY) ||
    localStorage.getItem(LEGACY_STORAGE_KEY) ||
    null
  );
}

/**
 * Stores the active JWT token in browser storage.
 */
export function setStoredToken(token) {
  if (typeof window === 'undefined') return;
  if (token) {
    localStorage.setItem(TOKEN_STORAGE_KEY, token);
    localStorage.setItem(LEGACY_STORAGE_KEY, token);
  } else {
    clearStoredToken();
  }
}

/**
 * Removes the JWT token from browser storage.
 */
export function clearStoredToken() {
  if (typeof window === 'undefined') return;
  localStorage.removeItem(TOKEN_STORAGE_KEY);
  localStorage.removeItem(LEGACY_STORAGE_KEY);
  localStorage.removeItem('chronomed_user');
}

// 1. Create configured Axios instance
const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  headers: {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  },
});

// 2. Request Interceptor: Inject Authorization Bearer token
api.interceptors.request.use(
  (config) => {
    const token = getStoredToken();
    if (token && !config.headers.Authorization) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
  },
  (error) => Promise.reject(error)
);

// 3. Response Interceptor: Handle 401 Unauthorized globally
api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response ? error.response.status : null;

    if (status === 401) {
      // Clear expired / invalid token
      clearStoredToken();

      // Redirect to /login if in browser and not already on the login page
      if (typeof window !== 'undefined') {
        const currentPath = window.location.pathname;
        if (currentPath !== '/login' && !currentPath.includes('/auth')) {
          console.warn('[API] 401 Unauthorized received. Redirecting to /login...');
          window.location.href = '/login';
        }
      }
    }

    return Promise.reject(error);
  }
);

export default api;
