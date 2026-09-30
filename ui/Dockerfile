# DIDA UI. Two shipping shapes off one dependency install:
#   * dev  — `vite dev` (HMR, source bind-mounted by compose). Used on the dev box.
#   * prod — a real `vite build` (adapter-node) served by server.js, which also
#            proxies /api → the API (WebSocket + camera streams included). Used in
#            production; nothing is bind-mounted, the build is baked in.
# compose selects the stage via the DIDA_UI_TARGET build arg (default: dev).
FROM node:24-bookworm-slim AS deps
WORKDIR /app
# The LOCK is the input, not a suggestion: `npm ci` installs exactly what CI
# type-checked and what we last built, so a transitive release can't quietly ship a
# different UI than the one that passed the gate. `npm install` resolved fresh on
# every build — reproducible only by luck.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund

# --- dev: Vite dev server, source comes from the compose bind mount ------------
FROM node:24-bookworm-slim AS dev
ENV NODE_ENV=development
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY ui/ ./
# Generate .svelte-kit/ (types + tsconfig) ahead of time so `vite dev` starts
# without the "cannot find ./.svelte-kit/tsconfig.json" warning.
RUN npx svelte-kit sync
EXPOSE 5173
CMD ["npm", "run", "dev"]

# --- build: produce the production bundle (adapter-node → /app/build) ----------
FROM node:24-bookworm-slim AS build
ENV NODE_ENV=production
# Address of the sister NVR, if this house runs one. Deployment-specific, so it
# is a build arg rather than a constant in the source: unset simply hides the
# "open in BABA" link (which is what the public demo wants).
ARG VITE_BABA_URL=""
ENV VITE_BABA_URL=$VITE_BABA_URL
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY ui/ ./
RUN npm run build

# --- prod: thin runtime serving the baked build + /api proxy (server.js) -------
# Reuses the same node_modules that built it (guaranteed runtime parity; the
# adapter-node output externalises `dependencies`, and server.js needs http-proxy).
FROM node:24-bookworm-slim AS prod
ENV NODE_ENV=production
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=build /app/build ./build
COPY ui/package.json ./package.json
COPY ui/server.js ./server.js
# NON-ROOT. This is the process behind the tunnel — it serves the SPA and proxies
# every /api call, so it is the most exposed thing we run, and it was running as
# uid 0 purely because the node image defaults to root. The image already ships an
# unprivileged `node` user (uid 1000) for exactly this. Nothing here is written at
# runtime (the build is baked, state lives in the API), so read-only ownership is
# all it needs.
RUN chown -R node:node /app
USER node
EXPOSE 5173
CMD ["node", "server.js"]
