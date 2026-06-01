FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HELIX_CONFIG=/opt/hlx-workflow-diff/config/hlx-diff.yaml

WORKDIR /opt/hlx-workflow-diff
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY config ./config

EXPOSE 8089
USER 1000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8089"]
