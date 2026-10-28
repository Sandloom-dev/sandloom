#!/bin/bash

# Start PostgreSQL test database for integration tests

set -e

echo "🐘 Starting PostgreSQL test database..."

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running. Please start Docker first."
    exit 1
fi

# Start test database
docker compose -f deploy/compose/test.yml up -d

# Wait for database to be ready
echo "⏳ Waiting for database to be ready..."
for i in {1..30}; do
    if docker exec sandloom_postgres_test pg_isready -U test_user -d sandloom_test > /dev/null 2>&1; then
        break
    fi
    sleep 1
done

if [ $? -eq 0 ]; then
    echo "✅ PostgreSQL test database is ready!"
    echo "📊 Database URL: postgresql+asyncpg://test_user:test_password@localhost:5433/sandloom_test"
    echo ""
    echo "🧪 To run tests:"
    echo "   uv run pytest src/sandloom_identity/tests/ -v"
    echo ""
    echo "🛑 To stop test database:"
    echo "   ./tools/checks/stop-test-db.sh"
else
    echo "❌ Failed to start PostgreSQL test database"
    exit 1
fi
