FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY fraudalert ./fraudalert
RUN pip install .
EXPOSE 8000
CMD ["fraudalert", "serve", "--host", "0.0.0.0", "--port", "8000"]
