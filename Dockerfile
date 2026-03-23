FROM python:3.10.20-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --upgrade pip
RUN pip install --no-cache-dir --target=/app -r /tmp/requirements.txt

COPY main.py /app/main.py
COPY action.yml /app/action.yml
COPY config.default.json /app/config.default.json
COPY app /app/app

FROM gcr.io/distroless/python3
COPY --from=builder /app /app
WORKDIR /app
ENV PYTHONPATH /app
CMD ["/app/main.py"]
