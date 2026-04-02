# 1. Use the Gaudi PyTorch installer as the base image for hardware acceleration
FROM vault.habana.ai/gaudi-docker/1.21.4/ubuntu22.04/habanalabs/pytorch-installer-2.6.0:latest

# 2. Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV MODULE_TOOLKIT_PATH=/app
ENV PYTHONPATH="${PYTHONPATH}:/app"

# 3. Set the working directory
WORKDIR /app

# 4. Install system dependencies and Docker CLI
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-20.10.24.tgz -o docker.tgz \
    && tar -xzf docker.tgz --strip-components=1 -C /usr/local/bin docker/docker \
    && rm docker.tgz \
    && chmod +x /usr/local/bin/docker

# 5. Install Jupyter alongside your requirements
RUN pip install --no-cache-dir jupyter

# 6. Copy requirements files first (for better Docker layer caching)
COPY requirements.txt /app/requirements.txt

# 7. Install Python dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# 8. Copy only the necessary application code. This ensures .env, etc. are ignored
COPY agents/ /app/agents/
COPY dockerfile/ /app/dockerfile/
COPY documentation/ /app/documentation/
COPY gpunit/ /app/gpunit/
COPY manifest/ /app/manifest/
COPY mcp/ /app/mcp/
COPY paramgroups/ /app/paramgroups/
COPY wrapper/ /app/wrapper/
COPY generate-module.py /app/

# 9. Set up directories
RUN mkdir -p /app/generated-modules