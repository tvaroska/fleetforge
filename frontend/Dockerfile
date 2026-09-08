# ============================================
# Stage 1: Development — the Vite dev server (used by docker-compose.override.yml)
# ============================================
FROM node:22-slim AS development

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

EXPOSE 8080

# 8080 in dev too, so the Traefik label needs no override.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "8080"]

# ============================================
# Stage 2: Builder
# ============================================
FROM node:22-slim AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ============================================
# Stage 3: Production — nginx serving the SPA and proxying /v1 to api:8000
# ============================================
FROM nginx:alpine AS production

# Writable temp/cache dirs so nginx can run unprivileged.
RUN mkdir -p /tmp/nginx && \
    chown -R nginx:nginx /tmp/nginx /var/log/nginx /var/cache/nginx && \
    chmod -R 755 /var/log/nginx

COPY --chown=nginx:nginx --from=builder /app/dist /usr/share/nginx/html
COPY --chown=nginx:nginx nginx.conf /etc/nginx/nginx.conf

USER nginx

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:8080/nginx-health || exit 1

CMD ["nginx", "-g", "daemon off;"]
