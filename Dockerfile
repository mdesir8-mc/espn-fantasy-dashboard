FROM python:3.12-slim
WORKDIR /app
COPY fantasy_tracker.py metrics.py webapp.py index.html ./
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python3", "webapp.py"]
