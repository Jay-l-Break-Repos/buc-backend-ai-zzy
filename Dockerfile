FROM python:3.13.7

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY repo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files needed for installation
COPY repo/pyproject.toml .
COPY repo/VERSION .

# Copy source code
COPY repo/src ./src
COPY repo/configs ./configs

# Install the package in development mode
RUN pip install -e .

# Create directories for logs and data
RUN mkdir -p /app/logs /app/volumes

# Expose the manager service port
EXPOSE 8081

# Set environment variables
ENV PYTHONPATH=/app/src

# Start the manager service
CMD ["python", "-m", "ai.backend.manager.server", "--config", "/app/configs/manager/halfstack.toml"]