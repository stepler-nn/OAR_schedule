/**
 * stores/auth.js — Pinia Store for Physician Authentication & RBAC Permissions.
 *
 * State:
 * - user: Physician profile (id, email, fullName, role, assignedHospitalId, skills)
 * - token: Active JWT access token
 * - isAuthenticated: Computed boolean (true when token & user exist)
 * - role: User RBAC tier ('DOCTOR' | 'SENIOR_RESIDENT' | 'HEAD_OF_DEPT')
 *
 * Actions:
 * - login(credentials): Authenticates with /api/v1/auth/login/json and saves JWT.
 * - logout(): Revokes stored token and redirects to /login.
 * - fetchCurrentUser(): Refreshes profile via /api/v1/auth/me.
 */

import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import api, { getStoredToken, setStoredToken, clearStoredToken } from '../services/api';

export const useAuthStore = defineStore('auth', () => {
  // 1. Reactive State
  const token = ref(getStoredToken());
  const user = ref(loadStoredUser());
  const isLoading = ref(false);
  const error = ref(null);

  function loadStoredUser() {
    if (typeof window === 'undefined') return null;
    try {
      const raw = localStorage.getItem('chronomed_user');
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function saveStoredUser(userData) {
    if (typeof window === 'undefined') return;
    if (userData) {
      localStorage.setItem('chronomed_user', JSON.stringify(userData));
    } else {
      localStorage.removeItem('chronomed_user');
    }
  }

  // 2. Computed Getters
  const isAuthenticated = computed(() => Boolean(token.value));
  const role = computed(() => user.value?.role || null);

  const isHeadOfDept = computed(() => role.value === 'HEAD_OF_DEPT');
  const isSeniorResident = computed(() => role.value === 'SENIOR_RESIDENT');
  const isDoctor = computed(() => role.value === 'DOCTOR');

  /**
   * Evaluates if current user can write/manage a specific hospital.
   * - HEAD_OF_DEPT: True across all hospitals.
   * - SENIOR_RESIDENT: True ONLY if assignedHospitalId === hospitalId.
   * - DOCTOR: False (read-only).
   */
  function canManageHospital(hospitalId) {
    if (isHeadOfDept.value) return true;
    if (isSeniorResident.value) {
      return Number(user.value?.assignedHospitalId) === Number(hospitalId);
    }
    return false;
  }

  // 3. Actions

  /**
   * Authenticates physician via email/password.
   * @param {{ email: string, password: string }} credentials
   */
  async function login({ email, password }) {
    isLoading.value = true;
    error.value = null;

    try {
      // Support JSON login endpoint
      const response = await api.post('/auth/login/json', {
        email: email.trim().toLowerCase(),
        password,
      });

      const data = response.data;
      const accessToken = data.access_token;

      // Update state & localStorage
      token.value = accessToken;
      setStoredToken(accessToken);

      user.value = {
        id: data.user_id,
        email: email.trim().toLowerCase(),
        fullName: data.full_name,
        role: data.role,
        assignedHospitalId: data.assigned_hospital_id,
        skills: data.skills || [],
      };
      saveStoredUser(user.value);

      return data;
    } catch (err) {
      const msg =
        err.response?.data?.detail ||
        err.message ||
        'Authentication failed. Please verify email and password.';
      error.value = typeof msg === 'string' ? msg : JSON.stringify(msg);
      throw new Error(error.value);
    } finally {
      isLoading.value = false;
    }
  }

  /**
   * Logs out the physician and purges stored credentials.
   */
  function logout() {
    token.value = null;
    user.value = null;
    error.value = null;
    clearStoredToken();
    saveStoredUser(null);

    if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
      window.location.href = '/login';
    }
  }

  /**
   * Refreshes the currently authenticated user's profile from the API.
   */
  async function fetchCurrentUser() {
    if (!token.value) {
      user.value = null;
      return null;
    }

    isLoading.value = true;
    try {
      const response = await api.get('/auth/me');
      const profile = response.data;

      user.value = {
        id: profile.id,
        email: profile.email,
        fullName: profile.full_name,
        role: profile.role,
        assignedHospitalId: profile.assigned_hospital_id,
        skills: profile.skills || [],
        contractWeeklyHours: profile.contract_weekly_hours,
        isActive: profile.is_active,
      };
      saveStoredUser(user.value);
      return user.value;
    } catch (err) {
      if (err.response?.status === 401) {
        logout();
      }
      throw err;
    } finally {
      isLoading.value = false;
    }
  }

  return {
    // State
    user,
    token,
    isLoading,
    error,

    // Getters
    isAuthenticated,
    role,
    isHeadOfDept,
    isSeniorResident,
    isDoctor,
    canManageHospital,

    // Actions
    login,
    logout,
    fetchCurrentUser,
  };
});
