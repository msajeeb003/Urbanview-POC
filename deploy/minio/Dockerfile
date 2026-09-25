# MinIO server built from source: MinIO stopped publishing free container images (Docker Hub and
# quay.io, 2025) and now distributes the community edition as source only
# (`go install github.com/minio/minio@<version>`). Pin MINIO_VERSION to a commit for repeatable
# builds; `latest` is the head of the default branch.
FROM golang:1.25-bookworm AS build
ARG MINIO_VERSION=latest
ENV CGO_ENABLED=0 GOTOOLCHAIN=auto
RUN go install "github.com/minio/minio@${MINIO_VERSION}"

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /go/bin/minio /usr/local/bin/minio
EXPOSE 9000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s \
  CMD curl -fsS http://localhost:9000/minio/health/live || exit 1
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
