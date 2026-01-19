dev:
    uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
build:
    cd frontend && npm run build
