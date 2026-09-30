# One worker, deliberately: queued simulations are held in the process's
# memory, so a poll must reach the worker that ran the job. Scaling out
# needs a shared job store first -- see the README.
#
# Rate limits key on the caller's address. Behind Render's proxy, set
# TRUSTED_PROXY_HOPS=1 in the environment so that is the caller's, not the
# proxy's; uvicorn's own --forwarded-allow-ips='*' would trust an address
# the caller wrote.
web: uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
