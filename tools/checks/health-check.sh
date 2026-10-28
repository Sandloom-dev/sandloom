#!/bin/bash

# Quick health check script for Sandloom development environment
# Usage: ./tools/checks/health-check.sh

echo "🏥 Sandloom Development Environment Health Check"
echo "=========================================="

# Check if we're in the right directory
if [ ! -f "pyproject.toml" ]; then
    echo "❌ Please run from the repository root"
    exit 1
fi

# Check UV
echo "📦 Checking UV package manager..."
if command -v uv &> /dev/null; then
    echo "✅ UV is installed: $(uv --version)"
else
    echo "❌ UV is not installed. Please install: https://github.com/astral-sh/uv"
    exit 1
fi

# Check Python environment
echo "🐍 Checking Python environment..."
if [ -d ".venv" ]; then
    echo "✅ Virtual environment exists"
    .venv/bin/python --version
else
    echo "⚠️  Virtual environment not found. Creating with UV..."
    uv sync
fi

# Check Docker
echo "🐳 Checking Docker..."
if command -v docker &> /dev/null; then
    echo "✅ Docker is installed: $(docker --version)"
else
    echo "❌ Docker is not installed"
    exit 1
fi

# Check if test services are running
echo "🧪 Checking test services..."
if docker ps --format '{{.Names}}' | grep -q "sandloom_postgres_test"; then
    echo "✅ PostgreSQL test container is running"
else
    echo "⚠️  PostgreSQL test container not running"
    echo "   Run: ./tools/checks/start-test-services.sh"
fi

if docker ps --format '{{.Names}}' | grep -q "sandloom_redis_test"; then
    echo "✅ Redis test container is running"
else
    echo "⚠️  Redis test container not running"
    echo "   Run: ./tools/checks/start-test-services.sh"
fi

# Check environment file
echo "⚙️  Checking environment configuration..."
if [ -f ".env.test" ]; then
    echo "✅ Test environment file exists"
else
    echo "❌ .env.test file missing"
    exit 1
fi

# Test Redis connection
echo "🗄️  Testing Redis connection..."
if docker exec sandloom_redis_test redis-cli ping &> /dev/null; then
    echo "✅ Redis connection successful"
else
    echo "⚠️  Redis connection failed"
fi

# Test PostgreSQL connection
echo "🐘 Testing PostgreSQL connection..."
if docker exec sandloom_postgres_test pg_isready -U test_user -d sandloom_test &> /dev/null; then
    echo "✅ PostgreSQL connection successful"
else
    echo "⚠️  PostgreSQL connection failed"
fi

# Run a quick test
echo "🧪 Running quick test..."
if uv run python -c "import src.sandloom.main; print('✅ Backend imports successfully')" &> /dev/null; then
    echo "✅ Backend application loads successfully"
else
    echo "❌ Backend application failed to load"
fi

echo ""
echo "🎉 Health check complete!"
echo ""
echo "Next steps:"
echo "1. Install VSCode extensions: Ctrl+Shift+P → 'Extensions: Show Recommended Extensions'"
echo "2. Start test services: Ctrl+Shift+P → 'Tasks: Run Task' → '🧪 Start Test Services'"
echo "3. Run tests: Ctrl+Shift+P → 'Tasks: Run Task' → '🚀 Run All Tests'"
echo "4. Debug tests: F5 → select '🧪 Debug Test'"
