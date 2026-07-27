FROM node:20-alpine
WORKDIR /app
COPY tools/design-gallery/server.js  tools/design-gallery/overlay.js  tools/design-gallery/overlay.css ./
COPY docs/design ./public
ENV GALLERY_DIR=/app/public \
    DB_PATH=/data/comments.json \
    PORT=8080
EXPOSE 8080
CMD ["node","server.js"]
