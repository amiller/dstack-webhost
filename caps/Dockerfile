FROM node:22-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY index.mjs demo-server.mjs accept.mjs test.mjs ./
EXPOSE 3500
CMD ["node", "--experimental-wasm-modules", "demo-server.mjs"]
