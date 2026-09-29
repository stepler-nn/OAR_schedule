import { createApp } from 'vue';
import { createPinia } from 'pinia';
import App from './App.vue';
import './style.css';
import { initChronoMedPWA } from './sw-register';

const app = createApp(App);
const pinia = createPinia();

app.use(pinia);
app.mount('#app');

// Register Workbox Service Worker & Offline TimeLog Queue Sync
initChronoMedPWA();
