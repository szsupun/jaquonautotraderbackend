# AI Signal Bot - Pocket Option
# Python 3.11 slim for smaller image
FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY *.py ./

# Run the bot
CMD ["python", "-u", "main.py"]