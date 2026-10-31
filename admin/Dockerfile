FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402 AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY patches ./patches
COPY scripts/dependency-compat.test.mjs ./scripts/dependency-compat.test.mjs
COPY scripts/apply-build-tool-patches.mjs ./scripts/apply-build-tool-patches.mjs
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:stable-alpine@sha256:f5116ed4606f3eb015dd4a8e28cb9b351476dc0ad4bc6e9f93b19cec130cdf03
# The pinned upstream image predates these Alpine security backports.
USER root
RUN apk add --no-cache --upgrade \
    brotli-libs=1.2.1-r0 curl=8.23.0-r0 libcurl=8.23.0-r0 \
    musl=1.2.6-r3 musl-utils=1.2.6-r3 zlib=1.3.2-r1
USER 101
ENV SANDLOOM_ADMIN_API_UPSTREAM=http://app:2222
ENV SANDLOOM_ADMIN_PUBLIC_SCHEME=http
ENV NGINX_ENVSUBST_FILTER=^SANDLOOM_ADMIN_
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
