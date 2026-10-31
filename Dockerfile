FROM node:22-alpine AS frontend

WORKDIR /fe

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY main.py ./
COPY login.py ./
COPY --from=frontend /static/dist ./static/dist

EXPOSE 8000

ENV SESSION_DIR=/sessions

CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]
