#!/bin/bash

# Script to start test infrastructure for Sandloom backend
# Usage: ./tools/checks/start-test-services.sh

echo "🚀 Starting Sandloom test infrastructure..."

# Check if docker-compose is available
if ! command -v docker-compose &> /dev/null; then
    echo "❌ docker-compose is required but not installed"
    exit 1
fi

# Start test services
echo "📦 Starting PostgreSQL and Redis for tests..."
docker-compose -f deploy/compose/test.yml up -d

# Wait for services to be ready
echo "⏳ Waiting for services to be ready..."

# Wait for PostgreSQL
echo "  🐘 Waiting for PostgreSQL..."
while ! docker exec sandloom_postgres_test pg_isready -U test_user -d sandloom_test &> /dev/null; do
    sleep 1
done
echo "  ✅ PostgreSQL is ready"

# Wait for Redis
echo "  🗄️  Waiting for Redis..."
while ! docker exec sandloom_redis_test redis-cli ping &> /dev/null; do
    sleep 1
done
echo "  ✅ Redis is ready"

echo ""
echo "🎉 Test infrastructure is ready!"
echo ""
echo "Services running:"
echo "  • PostgreSQL: localhost:5433 (database: sandloom_test)"
echo "  • Redis: localhost:6380 (databases 14-15 for tests)"
echo ""
echo "To run tests:"
echo "  uv run pytest src/sandloom_identity/tests/ -v"
echo ""
echo "To stop services:"
echo "  docker-compose -f deploy/compose/test.yml down"
