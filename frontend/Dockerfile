# 前端镜像：node 构建 → nginx 静态托管 + /api 反代到 api 服务。
# 构建上下文 = 项目根（见 deploy/compose.yaml：context: ..）。
FROM node:22-slim AS build
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
RUN npm run build

FROM nginx:1.27-alpine AS runtime
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
