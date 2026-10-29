FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && python -m patchright install --with-deps chrome \
    && rm -rf /var/lib/apt/lists/*

# Xvfb's ready signal must reach xvfb-run rather than container PID 1.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY main.py action.yml config.default.json /app/
COPY app /app/app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["xvfb-run", "-a", "python", "-u", "/app/main.py"]
