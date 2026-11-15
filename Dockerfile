# ORchestraAI web service (pages + API in one process), for Railway or any server with Docker.
# Railway builds with this file whenever it is in the repo (no build or start command to set).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/orchestra
COPY requirements.txt .
RUN pip install -r requirements.txt

# The app reads app/, orchestra_engine/, web/ and supabase/migrations/ (the status page runs missing SQL).
COPY . .
RUN useradd --system --uid 10001 --no-create-home orchestra
USER orchestra

EXPOSE 8000
# A shell, so the PORT that Railway sets is read when the container starts; exec, so uvicorn itself gets
# the stop signal and finishes the requests in flight.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
