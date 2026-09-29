<!--
  views/DoctorDashboard.vue — Mobile-First PWA Dashboard for Anesthesiology & ICU Physicians.
  Features:
  - Mobile-optimized, touch-friendly layout with iOS notch/home-indicator safe-area padding.
  - Offline connectivity banner (`navigator.onLine`) showing when Workbox cached schedules are active
    inside hospital surgical corridors or basement dead zones.
  - In-App PWA Install Prompt (`beforeinstallprompt` + iOS Safari "Add to Home Screen" sheet).
  - Card-based chronological list of the doctor's upcoming shifts (24h ICU Base Shifts & nested OR Tasks)
    with hospital badge, specific OR/ICU unit, UTC/local shift timings, and 32h `FATIGUE_RISK_HIGH` alerts.
  - Integrated `TimeTracker.vue` (PWA Check-In / Check-Out) and `SwapRequests.vue` (Peer Swap Inbox).
-->
<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue';
import { useScheduleStore } from '../stores/schedule';
import TimeTracker from '../components/TimeTracker.vue';
import SwapRequests from '../components/SwapRequests.vue';

const store = useScheduleStore();

// Active mobile tab: 'schedule' | 'tracker' | 'swaps'
const activeTab = ref('schedule');

// Network & PWA Install State
const isOnline = ref(typeof navigator !== 'undefined' ? navigator.onLine : true);
const deferredInstallPrompt = ref(null);
const isStandalone = ref(false);
const isIOS = ref(false);
const showIOSInstallModal = ref(false);

function updateOnlineStatus() {
  isOnline.value = navigator.onLine;
}

function handleBeforeInstallPrompt(e) {
  e.preventDefault();
  deferredInstallPrompt.value = e;
}

function handleAppInstalled() {
  isStandalone.value = true;
  deferredInstallPrompt.value = null;
}

async function triggerPWAInstall() {
  if (deferredInstallPrompt.value) {
    await deferredInstallPrompt.value.prompt();
    const { outcome } = await deferredInstallPrompt.value.userChoice;
    if (outcome === 'accepted') {
      isStandalone.value = true;
      deferredInstallPrompt.value = null;
    }
  } else if (isIOS.value) {
    showIOSInstallModal.value = true;
  }
}

onMounted(() => {
  window.addEventListener('online', updateOnlineStatus);
  window.addEventListener('offline', updateOnlineStatus);
  window.addEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
  window.addEventListener('appinstalled', handleAppInstalled);

  isStandalone.value =
    window.matchMedia('(display-mode: standalone)').matches ||
    window.navigator.standalone === true;
  isIOS.value = /iphone|ipad|ipod/i.test(window.navigator.userAgent);
});

onUnmounted(() => {
  window.removeEventListener('online', updateOnlineStatus);
  window.removeEventListener('offline', updateOnlineStatus);
  window.removeEventListener('beforeinstallprompt', handleBeforeInstallPrompt);
  window.removeEventListener('appinstalled', handleAppInstalled);
});

// Lookup helpers for hospital & workplace display
function getHospital(hospitalId) {
  return (
    store.hospitals.find((h) => h.id === hospitalId) || {
      code: `HOSP-${hospitalId}`,
      name: `Hospital #${hospitalId}`,
    }
  );
}

function getWorkplace(workplaceId) {
  return (
    store.workplaces.find((w) => w.id === workplaceId) || {
      code: `WP-${workplaceId}`,
      name: `Unit #${workplaceId}`,
      type: 'OR',
    }
  );
}

// Chronologically sorted upcoming Base Shifts + nested Child OR Tasks for the logged-in doctor
const myUpcomingShifts = computed(() => {
  const docId = store.currentUser.id;
  const baseShifts = store.shifts
    .filter(
      (s) =>
        s.doctorId === docId &&
        s.shiftType === 'BASE_SHIFT' &&
        s.status !== 'CANCELLED'
    )
    .sort((a, b) => a.startTs - b.startTs);

  return baseShifts.map((base) => {
    const childOrTasks = store.shifts
      .filter(
        (c) =>
          c.shiftType === 'OPERATIONAL_TASK' &&
          c.parentShiftId === base.id &&
          c.status !== 'CANCELLED'
      )
      .sort((a, b) => a.startTs - b.startTs);

    const startDate = new Date(base.startTs * 1000);
    const endDate = new Date(base.endTs * 1000);
    const dateLabel = startDate.toLocaleDateString('en-US', {
      weekday: 'short',
      month: 'short',
      day: '2-digit',
      timeZone: 'UTC',
    });
    const startClock = startDate.toISOString().slice(11, 16);
    const endClock = endDate.toISOString().slice(11, 16);
    const durationHours = Math.round((base.endTs - base.startTs) / 3600);

    return {
      ...base,
      hospital: getHospital(base.hospitalId),
      workplace: getWorkplace(base.workplaceId),
      dateLabel,
      timeRangeLabel: `${startClock} – ${endClock} UTC (${durationHours}h)`,
      durationHours,
      childOrTasks: childOrTasks.map((task) => {
        const tStart = new Date(task.startTs * 1000).toISOString().slice(11, 16);
        const tEnd = new Date(task.endTs * 1000).toISOString().slice(11, 16);
        return {
          ...task,
          workplace: getWorkplace(task.workplaceId),
          timeRangeLabel: `${tStart} – ${tEnd} UTC`,
          durationHours: Math.round((task.endTs - task.startTs) / 3600),
        };
      }),
    };
  });
});

// Count of incoming pending peer swap offers addressed to current doctor
const pendingIncomingSwapsCount = computed(
  () =>
    store.swapRequests.filter(
      (r) =>
        r.targetDoctorId === store.currentUser.id && r.status === 'PROPOSED'
    ).length
);
</script>

<template>
  <div
    class="min-h-screen bg-slate-950 text-slate-100 flex flex-col max-w-md mx-auto border-x border-slate-900 pt-[env(safe-area-inset-top)] pb-[calc(4.5rem+env(safe-area-inset-bottom))]"
  >
    <!-- 1. STICKY MOBILE HEADER -->
    <header
      class="sticky top-0 z-30 bg-slate-950/95 backdrop-blur border-b border-slate-800 px-4 py-3 flex items-center justify-between"
    >
      <div>
        <div class="flex items-center gap-2">
          <span class="text-sm font-semibold tracking-tight text-white">ChronoMed PWA</span>
          <span
            :class="[
              'w-2 h-2 rounded-full',
              isOnline ? 'bg-emerald-400' : 'bg-amber-400 animate-pulse',
            ]"
          ></span>
        </div>
        <p class="text-xs text-slate-400 truncate mt-0.5">
          {{ store.currentUser.fullName }} ·
          <span class="font-mono text-sky-400">{{ store.currentUser.role }}</span>
        </p>
      </div>

      <!-- In-App PWA Install Action (Hidden once installed in standalone mode) -->
      <button
        v-if="!isStandalone && (deferredInstallPrompt || isIOS)"
        type="button"
        @click="triggerPWAInstall"
        class="px-3 py-1.5 rounded-md bg-sky-500 text-slate-950 text-xs font-semibold hover:bg-sky-400 active:scale-95 transition"
      >
        Install App
      </button>
    </header>

    <!-- 2. OFFLINE HOSPITAL DEAD-ZONE BANNER -->
    <div
      v-if="!isOnline"
      class="bg-amber-950/80 border-b border-amber-700/70 px-4 py-2 text-xs text-amber-200 flex items-center justify-between"
    >
      <span>Offline Mode — Viewing cached schedule (Service Worker active).</span>
      <span class="font-mono text-[11px] text-amber-300">SW: NetworkFirst</span>
    </div>

    <!-- 3. MAIN VIEWPORT CONTENT -->
    <main class="flex-1 p-4 space-y-5">
      <!-- Quick Check-In / Check-Out Banner Always Accessible at Top of Schedule -->
      <TimeTracker v-if="activeTab === 'schedule' || activeTab === 'tracker'" :compact="activeTab === 'schedule'" />

      <!-- TAB A: UPCOMING SHIFTS & NESTED OR TASKS -->
      <section v-if="activeTab === 'schedule'" class="space-y-3">
        <div class="flex items-center justify-between">
          <h2 class="text-xs font-semibold uppercase tracking-wider text-slate-400">
            Upcoming Shifts &amp; OR Tasks ({{ myUpcomingShifts.length }})
          </h2>
          <button
            type="button"
            @click="activeTab = 'swaps'"
            class="text-xs font-medium text-sky-400 hover:text-sky-300"
          >
            + Request Swap
          </button>
        </div>

        <div
          v-if="myUpcomingShifts.length === 0"
          class="p-6 rounded-xl border border-slate-800 bg-slate-900/40 text-center text-xs text-slate-400"
        >
          No upcoming shifts scheduled for {{ store.currentUser.fullName }}.
        </div>

        <!-- Shift Cards -->
        <article
          v-for="shift in myUpcomingShifts"
          :key="shift.id"
          class="rounded-xl border border-slate-800 bg-slate-900/60 p-4 space-y-3 active:bg-slate-900 transition"
        >
          <div class="flex items-start justify-between gap-2">
            <div>
              <div class="flex items-center gap-2">
                <span
                  class="px-2 py-0.5 rounded bg-sky-500/15 border border-sky-500/40 text-sky-300 text-[11px] font-mono font-semibold"
                >
                  {{ shift.hospital.code }}
                </span>
                <span class="text-xs font-semibold text-white">{{ shift.dateLabel }}</span>
              </div>
              <h3 class="text-sm font-semibold text-slate-100 mt-1">
                {{ shift.workplace.name }}
              </h3>
              <p class="text-xs text-slate-400">{{ shift.hospital.name }}</p>
            </div>

            <span
              :class="[
                'px-2 py-0.5 rounded text-[11px] font-mono border',
                shift.status === 'PENDING_SWAP'
                  ? 'bg-violet-950/70 border-violet-500/60 text-violet-200'
                  : 'bg-emerald-950/50 border-emerald-700/50 text-emerald-300',
              ]"
            >
              {{ shift.status }}
            </span>
          </div>

          <!-- Shift Timing & 32h Fatigue Telemetry -->
          <div class="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-slate-800/80 text-xs">
            <span class="font-mono text-slate-300 tabular-nums">{{ shift.timeRangeLabel }}</span>
            <span
              v-if="shift.isFatigueRisk"
              class="px-2 py-0.5 rounded bg-amber-500/20 border border-amber-400/70 text-amber-200 text-[11px] font-mono font-semibold"
            >
              FATIGUE_RISK_HIGH ({{ shift.continuousHoursAtEnd }}h Continuous)
            </span>
          </div>

          <!-- Nested Child OR Tasks inside 24h Base Shift -->
          <div v-if="shift.childOrTasks.length > 0" class="space-y-1.5 pt-2 border-t border-slate-800/60">
            <div class="text-[11px] font-medium text-slate-400">
              Assigned Operating Room Sub-Tasks ({{ shift.hospital.code }}):
            </div>
            <div
              v-for="task in shift.childOrTasks"
              :key="task.id"
              class="p-2.5 rounded-lg bg-slate-950/90 border border-slate-800 flex items-center justify-between text-xs"
            >
              <div>
                <div class="font-semibold text-slate-200">↳ {{ task.workplace.name }}</div>
                <div class="text-[11px] text-slate-400">{{ task.notes || 'Surgical Anesthesia Block' }}</div>
              </div>
              <span class="font-mono text-[11px] text-sky-300 tabular-nums">
                {{ task.timeRangeLabel }}
              </span>
            </div>
          </div>
        </article>
      </section>

      <!-- TAB C: PEER SHIFT SWAP INBOX & PROPOSAL FLOW -->
      <SwapRequests v-if="activeTab === 'swaps'" />
    </main>

    <!-- 4. BOTTOM TOUCH NAVIGATION BAR (iOS Safe-Area Compliant) -->
    <nav
      class="fixed bottom-0 left-0 right-0 z-40 max-w-md mx-auto bg-slate-950/95 backdrop-blur border-t border-slate-800 pb-[env(safe-area-inset-bottom)]"
    >
      <div class="grid grid-cols-3 h-16">
        <button
          type="button"
          @click="activeTab = 'schedule'"
          :class="[
            'flex flex-col items-center justify-center gap-1 text-xs font-medium transition-colors',
            activeTab === 'schedule' ? 'text-sky-400' : 'text-slate-400 hover:text-slate-200',
          ]"
        >
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
          <span>My Shifts</span>
        </button>

        <button
          type="button"
          @click="activeTab = 'tracker'"
          :class="[
            'flex flex-col items-center justify-center gap-1 text-xs font-medium transition-colors',
            activeTab === 'tracker' ? 'text-sky-400' : 'text-slate-400 hover:text-slate-200',
          ]"
        >
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <span>Check-In/Out</span>
        </button>

        <button
          type="button"
          @click="activeTab = 'swaps'"
          :class="[
            'relative flex flex-col items-center justify-center gap-1 text-xs font-medium transition-colors',
            activeTab === 'swaps' ? 'text-sky-400' : 'text-slate-400 hover:text-slate-200',
          ]"
        >
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4" />
          </svg>
          <span>Swaps</span>
          <span
            v-if="pendingIncomingSwapsCount > 0"
            class="absolute top-2 right-8 w-4 h-4 rounded-full bg-rose-500 text-white text-[10px] font-mono font-bold flex items-center justify-center"
          >
            {{ pendingIncomingSwapsCount }}
          </span>
        </button>
      </div>
    </nav>

    <!-- iOS Safari Install Guide Sheet -->
    <div
      v-if="showIOSInstallModal"
      class="fixed inset-0 z-50 bg-slate-950/80 flex items-end justify-center p-4"
    >
      <div class="w-full max-w-sm rounded-2xl bg-slate-900 border border-slate-800 p-5 space-y-3">
        <h3 class="text-sm font-semibold text-white">Install ChronoMed on iPhone</h3>
        <ol class="text-xs text-slate-300 space-y-1.5 list-decimal list-inside">
          <li>Tap the <strong>Share</strong> icon in Safari's bottom bar.</li>
          <li>Scroll down and tap <strong>Add to Home Screen</strong>.</li>
          <li>Launch ChronoMed directly from your home screen with offline caching.</li>
        </ol>
        <button
          type="button"
          @click="showIOSInstallModal = false"
          class="w-full py-2 rounded-lg bg-slate-800 text-xs font-semibold text-white"
        >
          Got It
        </button>
      </div>
    </div>
  </div>
</template>
