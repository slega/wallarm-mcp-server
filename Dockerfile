FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY wallarm_mcp_server.py .
COPY wallarm-swagger-documented-fixed.json .

# Run the server
CMD ["python", "wallarm_mcp_server.py"]

