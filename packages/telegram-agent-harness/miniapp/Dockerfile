FROM oven/bun:1.3.1-alpine
WORKDIR /app
COPY relay.ts index.html app.js client.js style.css ./
USER bun
EXPOSE 3000
CMD ["bun", "relay.ts"]
