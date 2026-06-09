FROM python:3.12-slim

WORKDIR /app

RUN useradd -m weasel && \
    mkdir -p /app/data && \
    chown weasel /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=weasel . .

USER weasel

EXPOSE 5000

CMD ["python", "app.py"]
