FROM python:3.11-slim

# Install DNS tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsutils \
    bind9-dnsutils \
    krb5-user \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for all providers
RUN pip install --no-cache-dir \
    requests \
    hvac \
    boto3 \
    google-cloud-dns \
    google-auth

# Create non-root user
RUN useradd -m -s /bin/bash appuser

# Create directories
RUN mkdir -p /app /state /secrets && chown -R appuser:appuser /app /state

WORKDIR /app
COPY dns_failover.py /app/
COPY setup.py /app/

USER appuser

ENTRYPOINT ["python3", "/app/dns_failover.py"]
CMD ["run"]
