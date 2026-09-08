FROM python:3.11-slim

WORKDIR /app

# Install pinned dependencies BEFORE copying the source, so editing a .py file
# does not invalidate this layer and force a full reinstall on every rebuild.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy folder contents. .dockerignore keeps .env, *.pem and the live database
# out of the image; those arrive at runtime through the bind mount.
COPY . /app

# Run the scheduler unbuffered so logs stream in real-time
CMD ["python", "-u", "run_loop.py"]
