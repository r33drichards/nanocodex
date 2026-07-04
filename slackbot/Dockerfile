# nanocodex Slack bot (Socket Mode — outbound only, no ports to publish).
FROM node:22-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY index.ts threads.ts tsconfig.json ./

CMD ["npx", "tsx", "index.ts"]
