FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python deps first (layer cache)
COPY backend/requirements.txt ./backend/

# Install CPU-only PyTorch first to avoid pulling in multi-GB CUDA packages
# (camel-oasis/camel-ai depend on torch but MiroFish only needs CPU inference)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy all MiroFish-main source (build context = MiroFish-main/)
COPY . .

# Install the MiroFish client SDK (client/) and cart_recovery module
RUN pip install --no-cache-dir ./client

# cart_recovery and client modules live at MiroFish-main root — already copied above
ENV PYTHONPATH=/app
ENV FLASK_APP=backend/run.py

WORKDIR /app/backend

EXPOSE ${PORT:-5001}

# Use gunicorn in production — long timeout for MiroFish analysis (up to 15 min)
# Railway sets $PORT; fall back to 5001 for local Docker usage
CMD gunicorn --bind "0.0.0.0:${PORT:-5001}" --workers 2 --timeout 900 --preload run:app
