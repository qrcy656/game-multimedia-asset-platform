FROM node:22-alpine
ENV NODE_ENV=production PORT=8080 DATA_DIR=/data VENDOR_DIR=/app/vendor FRONTEND_DIR=/app/frontend
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY server.mjs render.mjs renderer-worker.mjs ./
COPY vendor/ ./vendor/
COPY frontend/ ./frontend/
COPY LICENSE UPSTREAM.md ./
USER 1007:1007
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 CMD node -e "fetch('http://127.0.0.1:8080/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"
CMD ["node", "server.mjs"]
