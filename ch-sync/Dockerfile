FROM python:3.11-slim

WORKDIR /app
COPY common /app/common
COPY ch-sync/ch_sync.py /app/ch_sync.py

EXPOSE 8080
CMD ["python3", "-u", "/app/ch_sync.py"]
