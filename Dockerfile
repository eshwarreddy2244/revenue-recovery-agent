# AI Revenue Recovery Agent -- Streamlit dashboard container
FROM python:3.12-slim

WORKDIR /app

# System deps kept minimal -- fpdf2/streamlit/razorpay/groq are all pure-Python.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The container has no persisted volume by default -- data/audit.db and
# data/synthetic_events.json will regenerate on first run inside the
# container. Mount ./data as a volume if you want them to persist
# across container restarts.
EXPOSE 8501

HEALTHCHECK CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
