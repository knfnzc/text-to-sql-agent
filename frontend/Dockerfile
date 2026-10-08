# syntax=docker/dockerfile:1

# ─────────────────────────────────────────────────────────────
# 前端镜像：Node 构建 + Nginx 托管静态产物
#
# 构建（在 frontend/ 目录下执行）：
#     docker build -t text-to-sql-agent-frontend .
# 运行：
#     docker run --rm -p 8080:80 text-to-sql-agent-frontend
#
# 默认产物是「离线演示模式」：不连后端也能看到完整界面与执行过程回放。
# Vite 的变量是在构建期写进产物的，所以想连真实后端要在构建时传：
#     docker build \
#       --build-arg VITE_DEMO=false \
#       --build-arg VITE_API_BASE=http://localhost:8000/api \
#       -t text-to-sql-agent-frontend .
# 注意此时浏览器直连后端属于跨域，需要把前端地址加进后端的 CORS_ORIGINS。
# ─────────────────────────────────────────────────────────────

# ---------- 构建阶段 ----------
FROM node:22-alpine AS build

WORKDIR /app

# 只拷清单文件再装依赖，业务代码改动不会让依赖层缓存失效
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# 构建期注入。默认值就是"静态演示"这一档，保证裸 docker run 也能看到东西
ARG VITE_DEMO=true
ARG VITE_API_BASE=/api
ENV VITE_DEMO=$VITE_DEMO \
    VITE_API_BASE=$VITE_API_BASE

RUN npm run build

# ---------- 运行阶段 ----------
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
