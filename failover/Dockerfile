FROM alpine:3.20 AS privilege-helper
RUN apk add --no-cache build-base
COPY services/runtime/drop_privileges.c /tmp/drop_privileges.c
RUN cc -static -Os -s -Wall -Wextra -Werror \
    -o /drop-privileges /tmp/drop_privileges.c

FROM python:3.11-slim

# Install DNS tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsutils \
    bind9-dnsutils \
    krb5-user \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for all providers
COPY failover/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Create non-root user
RUN useradd -m -s /bin/bash appuser

# Create directories
RUN mkdir -p /app /state /secrets /scripts \
    && chown -R appuser:appuser /app /state

WORKDIR /app
COPY common /app/common
COPY failover/dns_failover.py failover/setup.py /app/
COPY --from=privilege-helper /drop-privileges /usr/local/bin/drop-privileges
COPY failover/secure-entrypoint.sh /usr/local/bin/secure-failover-entrypoint.sh
RUN chmod 0755 \
    /usr/local/bin/drop-privileges \
    /usr/local/bin/secure-failover-entrypoint.sh

USER root

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('STATUS_PORT', '8080') + '/readyz', timeout=2)"

ENTRYPOINT ["/usr/local/bin/secure-failover-entrypoint.sh"]
CMD ["run"]
