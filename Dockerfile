FROM python:3.12-slim

WORKDIR /app
COPY app /app/app
COPY config.gatchina.example.json /app/config.gatchina.example.json

RUN useradd --system --create-home monitor && mkdir -p /app/data && chown -R monitor:monitor /app
USER monitor

CMD ["python", "-m", "app.main"]
