FROM oven/bun:1.3.1-alpine
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=8080
COPY packages/telegram-agent-harness/miniapp/relay.ts packages/telegram-agent-harness/miniapp/relay.ts
COPY packages/telegram-agent-harness/week packages/telegram-agent-harness/week
USER bun
EXPOSE 8080
CMD ["bun", "packages/telegram-agent-harness/week/server.ts"]
