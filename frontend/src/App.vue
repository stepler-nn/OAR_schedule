<script setup>
import { ref } from 'vue';
import { useScheduleStore } from './stores/schedule';
import ScheduleMatrix from './components/ScheduleMatrix.vue';
import DoctorDashboard from './views/DoctorDashboard.vue';

const store = useScheduleStore();

// Top-level workspace switcher: 'matrix' (Desktop Resource-Timeline) | 'pwa' (Mobile Doctor PWA)
const activeSurface = ref('matrix');
</script>

<template>
  <div class="min-h-screen bg-slate-950 text-slate-100 flex flex-col">
    <!-- Global Navigation Bar Switching Between Desktop Schedule Matrix & Mobile Doctor PWA -->
    <div
      class="bg-slate-900/90 border-b border-slate-800 px-4 py-2 flex flex-wrap items-center justify-between gap-3 text-xs"
    >
      <div class="flex items-center gap-2">
        <span class="font-mono font-bold text-sky-400">CHRONOMED v1.0</span>
        <span class="text-slate-600">|</span>
        <span class="text-slate-400">Anesthesiology &amp; ICU Scheduling Platform</span>
      </div>

      <div class="flex items-center gap-2">
        <button
          type="button"
          @click="activeSurface = 'matrix'"
          :class="[
            'px-3 py-1 rounded-md font-medium transition-colors',
            activeSurface === 'matrix'
              ? 'bg-sky-500 text-slate-950 font-semibold'
              : 'bg-slate-800 text-slate-300 hover:text-white',
          ]"
        >
          Desktop Schedule Matrix
        </button>
        <button
          type="button"
          @click="activeSurface = 'pwa'"
          :class="[
            'px-3 py-1 rounded-md font-medium transition-colors',
            activeSurface === 'pwa'
              ? 'bg-sky-500 text-slate-950 font-semibold'
              : 'bg-slate-800 text-slate-300 hover:text-white',
          ]"
        >
          Doctor Mobile PWA
        </button>
      </div>
    </div>

    <!-- Active View -->
    <ScheduleMatrix v-if="activeSurface === 'matrix'" />
    <DoctorDashboard v-else />
  </div>
</template>
