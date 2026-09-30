# One worker, deliberately: queued simulations are held in the process's
# memory, so a poll must reach the worker that ran the job. Scaling out
# needs a shared job store first -- see the README.
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
