FROM alpine:3.20 AS privilege-helper
RUN apk add --no-cache build-base
COPY services/runtime/drop_privileges.c /tmp/drop_privileges.c
RUN cc -static -Os -s -Wall -Wextra -Werror \
    -o /drop-privileges /tmp/drop_privileges.c

FROM grafana/grafana:11.1.0
USER root
ENV GF_PATHS_PLUGINS=/var/lib/grafana-plugins
COPY --from=privilege-helper /drop-privileges /usr/local/bin/drop-privileges
COPY services/grafana/secure-entrypoint.sh /usr/local/bin/secure-grafana-entrypoint.sh
RUN mkdir -p "$GF_PATHS_PLUGINS" \
    && grafana cli --pluginsDir "$GF_PATHS_PLUGINS" plugins install \
      grafana-clickhouse-datasource 4.14.1 \
    && chown -R grafana:root "$GF_PATHS_PLUGINS" \
    && chmod 0755 /usr/local/bin/drop-privileges /usr/local/bin/secure-grafana-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/secure-grafana-entrypoint.sh"]
