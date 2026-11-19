#!/usr/bin/env bash
set -euo pipefail

echo "=== AITU Moodle Telegram Notifier: AWS Server Setup ==="

# 1. Update packages and install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo usermod -aG docker "$USER"
    echo "Docker installed successfully."
fi

# 2. Ensure .env file exists
if [ ! -f .env ]; then
    echo "ERROR: .env file not found in current directory!"
    echo "Please create .env with your TELEGRAM_BOT_TOKEN, NEON DATABASE_URL, etc."
    exit 1
fi

# 3. Build and launch container
echo "Building and starting container..."
docker compose build
docker compose up -d

echo ""
echo "=== Deployment complete! ==="
echo "Check logs with: docker compose logs -f bot"
echo "Check container status: docker compose ps"
