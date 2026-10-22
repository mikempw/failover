FROM python:3.11-slim

WORKDIR /app
COPY common /app/common
COPY vm-sync/vm_sync.py /app/vm_sync.py

EXPOSE 8080
CMD ["python3", "-u", "/app/vm_sync.py"]
