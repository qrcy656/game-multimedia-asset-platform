import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const communityOrigin = process.env.FANTASY_COMMUNITY_URL || 'http://127.0.0.1:18082';
const gatewayPaths = ['/narrative/api', '/api', '/auth', '/login', '/logout', '/community.js', '/portal-static'];

export default defineConfig({
  base: '/narrative/',
  plugins: [react()],
  build: { outDir: 'static', emptyOutDir: true, sourcemap: false },
  server: { proxy: Object.fromEntries(gatewayPaths.map(path => [path, { target: communityOrigin }])) },
});
