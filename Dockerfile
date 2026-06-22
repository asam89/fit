FROM python:3.12-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Create data directory
RUN mkdir -p data

ENV DATABASE_PATH=/app/data/fitnessbot.db
ENV DASHBOARD_HOST=0.0.0.0
ENV DASHBOARD_PORT=8000

EXPOSE 8000

CMD ["python", "-m", "fitnessbot.main"]
