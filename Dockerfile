FROM python:3.12-slim

WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    MF_HOST=0.0.0.0

# App deps + the data-layer engine (public repo) installed as a package, so no
# local sportsdata-mcp checkout is needed in the container.
COPY requirements.txt .
RUN pip install -r requirements.txt \
    "git+https://github.com/DanielTomaro13/sportsdata-mcp"

COPY . .

EXPOSE 8000
CMD ["python", "run.py"]
