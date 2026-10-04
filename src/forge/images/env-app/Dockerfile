FROM python:3.11-slim
RUN pip install --no-cache-dir 'psycopg[binary]' redis
COPY app.py /app.py
CMD ["python", "/app.py"]
