FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY static ./static
RUN useradd --uid 10001 --create-home terminal && mkdir /data && chown terminal:terminal /data
USER terminal
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=2)" || exit 1
CMD ["python", "server.py"]
