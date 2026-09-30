# DIDA Matter bridge — Node/TS service on matter.js. Not on dida/base (that's
# Python); Matter device-side tooling is JS, so this is our one Node service
# besides the UI.
FROM node:24-slim

# Drop root: the node:slim image ships a uid-1000 `node` user. mDNS + 5540 are
# non-privileged ports, and the bridge keeps no /state. The tree is installed as
# that user rather than chowned afterwards: a recursive chown copies all of
# node_modules into one more layer, which took 35 minutes on a slow disk.
WORKDIR /app
RUN chown node:node /app
USER node
# npm ci against a COMMITTED lockfile: `npm install` with no lockfile re-resolved
# matter.js and every transitive dep on each rebuild, so two builds of the same
# commit could ship different bridge code to the controllers.
COPY --chown=node:node services/matter-bridge/package.json services/matter-bridge/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --chown=node:node services/matter-bridge/ ./

# Matter listens on 5540 (operational) + advertises over mDNS; the container
# runs with host networking so a Matter controller on the LAN can discover it.
CMD ["npx", "tsx", "src/index.ts"]
