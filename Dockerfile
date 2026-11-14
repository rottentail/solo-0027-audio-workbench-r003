FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --gid audio --create-home audio && mkdir /data && chown audio:audio /data
COPY audio_workbench ./audio_workbench
USER audio
ENV AUDIO_WORKBENCH_DATA=/data
EXPOSE 8765
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).read()"
CMD ["python", "-m", "audio_workbench.server", "--host", "0.0.0.0"]
