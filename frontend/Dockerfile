# =====================================================================
# VivoGuard AI — frontend image
# Stage 1: build the Vite bundle.
# Stage 2: serve it from a tiny Nginx with HTML5 history-mode rewrites.
# =====================================================================
FROM node:20-alpine AS build
WORKDIR /app
# Cache npm deps separately from source.
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:alpine
# History-mode SPA: any unknown path falls back to index.html.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
