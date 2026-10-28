#!/bin/bash

# Stop PostgreSQL test database

set -e

echo "🛑 Stopping PostgreSQL test database..."

# Stop and remove test database containers
docker compose -f deploy/compose/test.yml down

echo "✅ PostgreSQL test database stopped"
