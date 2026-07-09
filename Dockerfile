FROM python:3.12-alpine

RUN pip install --no-cache-dir aiohttp paho-mqtt

COPY app /app
VOLUME /data
EXPOSE 8099

CMD ["python", "/app/main.py"]
