#!/bin/bash

# Quick test runner with HTML report
# Usage: ./tools/checks/run-tests-with-report.sh

echo "🧪 Running Sandloom tests with coverage report..."

# Create reports directory if it doesn't exist
mkdir -p reports

# Run tests with coverage and HTML report
uv run pytest \
    src/sandloom_identity/tests/ \
    --cov=src \
    --cov-report=html:reports/coverage \
    --cov-report=term-missing \
    --html=reports/test-report.html \
    --self-contained-html \
    -v

# Check if tests passed
if [ $? -eq 0 ]; then
    echo ""
    echo "🎉 All tests passed!"
    echo "📊 Coverage report: reports/coverage/index.html"
    echo "📝 Test report: reports/test-report.html"
    echo ""
    echo "To view reports:"
    echo "  open reports/coverage/index.html"
    echo "  open reports/test-report.html"
else
    echo ""
    echo "❌ Some tests failed. Check the reports for details."
    echo "📊 Coverage report: reports/coverage/index.html"
    echo "📝 Test report: reports/test-report.html"
fi
