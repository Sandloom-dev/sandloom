# Cachegrind image for the ladder: Python 3.13 + valgrind + the EXACT
# benchmark lock (requirements.txt = `uv export --no-dev --no-emit-project
# --no-hashes` of benchmarks/uv.lock, minus the editable framework, which is
# mounted read-only and put on PYTHONPATH by run_cg.sh).
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends valgrind \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /w
