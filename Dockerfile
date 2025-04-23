FROM python:3.8-buster

# Tizim bog'liqliklarini o'rnatish
RUN apt-get update && apt-get install -y \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev \
    gcc \
    libc-dev \
    && rm -rf /var/lib/apt/lists/*

# pip va wheel ni yangilash
RUN pip install --upgrade pip wheel

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "aigold_optimized_v8.py"]
