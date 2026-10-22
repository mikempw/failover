FROM busybox:1.36.1-musl AS probe

FROM alpine:3.20 AS privilege-helper
RUN apk add --no-cache build-base
COPY services/runtime/drop_privileges.c /tmp/drop_privileges.c
RUN cc -static -Os -s -Wall -Wextra -Werror \
    -o /drop-privileges /tmp/drop_privileges.c

FROM ghcr.io/f5devcentral/application-study-tool/otel_custom_collector:v0.9.6

# The pinned collector image is scratch-based and does not include the
# health_check extension. Add a static probe client and health-check its native
# Prometheus telemetry endpoint instead.
COPY --from=probe /bin/busybox /bin/busybox
COPY --from=privilege-helper /drop-privileges /usr/local/bin/drop-privileges
COPY services/otel_collector/bootstrap-gate.sh /otel-bootstrap-gate.sh

USER root

ENTRYPOINT ["/bin/busybox", "sh", "/otel-bootstrap-gate.sh"]
