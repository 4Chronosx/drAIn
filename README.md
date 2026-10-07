<h1 align="center">drAIn Backend 🌧️</h1>
<a id="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
<a href="https://github.com/eliseoalcaraz/drAIn-backend/blob/main/LICENSE">
<img alt="License" src="https://img.shields.io/badge/License-GPL--2.0-blue?style=for-the-badge" />
</a>

<div align="center">
  <a href="https://github.com/4Chronosx/drAIn">
    <img src="logo.png" alt="drAIn Backend logo" width="40%" height="35%">
  </a>
  <br />
  <p align="center">
    <a href="#"><img alt="Status" src="https://img.shields.io/badge/status-Beta-yellow?style=flat&color=yellow" /></a>
    <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white&style=flat" /></a>
    <a href="https://github.com/4Chronosx/drAIn/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/4Chronosx/drAIn?color=coral&logo=git&logoColor=white" /></a>
  </p>
  <a href="https://github.com/4Chronosx/drAIn/issues/new?labels=bug&template=bug-report---.md">Report Bug</a>
  &middot;
  <a href="https://github.com/4Chronosx/drAIn/issues/new?labels=enhancement&template=feature-request---.md">Request Feature</a>
</div>

---

## 🗺️ Overview

The **drAIn Backend** powers the simulation engine and API infrastructure for the drAIn platform. Built with **FastAPI** and **PySWMM**, it provides RESTful endpoints for running **SWMM (Storm Water Management Model)** hydraulic simulations, processing drainage system data, and returning simulated flood hazard results to the frontend on request. The model has not been calibrated against field records yet (see `docs/SCIENCE_ROADMAP.md`), and every result says so in `metadata.model_info`.

This backend transforms complex hydrological modeling into accessible API services, enabling engineers and planners to run sophisticated urban drainage simulations through simple HTTP requests.

---

### 💡 Why This Backend?

Urban flood modeling typically requires specialized software and technical expertise. The drAIn backend democratizes access to SWMM simulations by:

* **Abstracting Complexity**: Wraps SWMM's Python API in intuitive REST endpoints
* **Enabling On-Demand Simulation**: Runs "what-if" scenarios for infrastructure planning in the background and returns them when done
* **Processing at Scale**: Handles data preprocessing and result analysis automatically
* **Cloud-Ready Architecture**: Deployed on Render for reliable, scalable API access

> **Scope.** This serves one hand-built SWMM model of Mandaue. The API is
> the cheap part; the expensive part is the calibrated network behind it —
> invert levels, pipe geometry, catchment delineation, survey work — which
> is months of engineering per city. Another city needs that model built
> first, not just a redeploy.

#### ⚙️ Core Capabilities

* 🌊 **SWMM Simulation Engine**: Python-based hydraulic and hydrological modeling using PySWMM
* 🚀 **Queued API**: FastAPI endpoints that queue simulations and let clients poll for results; runs execute one at a time by default
* 📊 **Data Pipeline**: Automated preprocessing of raw drainage data for SWMM inputs
* 🔄 **Scenario Management**: Support for multiple rainfall scenarios and infrastructure configurations
* 📈 **Result Analytics**: Post-processing of simulation outputs for flood hazard ranking

---

## 📚 Tech Stack

### Core Framework
<p align="left">
  <a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white&style=flat" /></a>
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white&style=flat" /></a>
  <a href="https://www.uvicorn.org/"><img alt="Uvicorn" src="https://img.shields.io/badge/Uvicorn-2094F3?logo=gunicorn&logoColor=white&style=flat" /></a>
</p>

### Simulation & ML
<p align="left">
  <a href="https://www.epa.gov/water-research/storm-water-management-model-swmm"><img alt="PySWMM" src="https://img.shields.io/badge/PySWMM-0078D4?logo=python&logoColor=white&style=flat" /></a>
  <a href="https://numpy.org/"><img alt="NumPy" src="https://img.shields.io/badge/NumPy-013243?logo=numpy&logoColor=white&style=flat" /></a>
</p>

### Data Processing
<p align="left">
  <a href="https://colab.research.google.com/"><img alt="Google Colab" src="https://img.shields.io/badge/Google_Colab-F9AB00?logo=googlecolab&logoColor=white&style=flat" /></a>
  <a href="https://jupyter.org/"><img alt="Jupyter" src="https://img.shields.io/badge/Jupyter-F37626?logo=jupyter&logoColor=white&style=flat" /></a>
</p>

### Deployment
<p align="left">
  <a href="https://render.com/"><img alt="Render" src="https://img.shields.io/badge/Render-46E3B7?logo=render&logoColor=white&style=flat" /></a>
</p>

---

## 📁 Project Structure

```
drAIn-backend/
├── app/                   # HTTP layer (FastAPI)
│   ├── auth.py            # Checks Supabase access tokens
│   ├── cache.py           # A bounded, expiring cache
│   ├── config.py          # Settings read from the environment
│   ├── isolation.py       # Runs a simulation in a child process it can kill
│   ├── jobs.py            # The run queue
│   ├── logging_config.py  # Logging setup
│   ├── main.py            # App factory, CORS, routes
│   ├── middleware.py      # Body size limit, per-address rate limit, headers
│   ├── polling.py         # Finished results, serialised once, with ETags
│   ├── runs.py            # The durable record of runs in Supabase
│   ├── schemas.py         # Request/response models
│   └── simulation.py      # A request to its payload; the shared baseline
├── drain/                 # Domain logic — no web framework imports
│   ├── cli.py             # Run a simulation without the server
│   ├── exposure.py        # Population around a node, by barangay
│   ├── flooding.py        # Assembles the API payload
│   ├── geo.py             # UTM reprojection, point-in-polygon
│   ├── hazard.py          # Flood hazard scoring
│   ├── network.py         # Node locations, read from the .inp
│   ├── paths.py           # Locations of the bundled model files
│   ├── rainfall.py        # Design-storm generation
│   ├── rpt_parser.py      # Parses SWMM .rpt reports
│   ├── swmm_runner.py     # Runs SWMM
├── scripts/               # Analysis, not imported by the server
│   └── validate_against_reports.py
├── data/                  # SWMM network and trained model
│   ├── Mandaue_Drainage_Network.inp
│   ├── Mandaue_Drainage_Network.out
│   ├── Mandaue_Drainage_Network.rpt
│   ├── mandaue_population.geojson
├── tests/                 # pytest suite
├── Python_Notebooks/      # Data preprocessing notebooks
│   ├── INP_FILE_GENERATOR.ipynb
│   └── KMEANS_MODEL.ipynb
├── Procfile               # Process configuration
├── pyproject.toml         # ruff and pytest configuration
├── requirements.in        # Direct runtime dependencies
├── requirements.txt       # ...compiled: every package pinned and hashed
├── requirements-dev.in    # Adds pytest, ruff and scipy
└── requirements-dev.txt   # ...compiled the same way
```

---

## 💻 Getting Started

Follow these steps to set up and run the **drAIn Backend** locally.

### 🔧 Prerequisites

Make sure you have installed:

- [Python](https://www.python.org/) (v3.12+)
- [pip](https://pip.pypa.io/) or [conda](https://docs.conda.io/)

The SWMM engine ships with `pyswmm`, so no separate install is needed.

---

### 🛠️ Installation

```bash
# Clone the repository
git clone https://github.com/4Chronosx/drAIn.git
cd drAIn

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install --require-hashes -r requirements.txt

# ...or, to also get pytest and ruff
pip install --require-hashes -r requirements-dev.txt
```

Every package, direct or not, is pinned to a version and to the hashes of
its published files, so an install gets exactly what was tested and a
tampered download fails. To change a dependency, edit `requirements.in` (or
`requirements-dev.in`) and recompile both:

```bash
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --strip-extras requirements.in
pip-compile --generate-hashes --allow-unsafe --strip-extras requirements-dev.in
```

---

### 🚀 Running the API

```bash
# Start the FastAPI server
uvicorn app.main:app --reload

# Server will be available at http://localhost:8000
# Interactive API docs at http://localhost:8000/docs, with ENABLE_DOCS=true
```

To run a simulation without starting the server:

```bash
python -m drain.cli --precip 400 --duration 24 --node I-4
```

### ✅ Tests and linting

```bash
pytest
ruff check app drain tests scripts
ruff format --check app drain tests scripts
```

### 🔧 Configuration

All optional; the defaults cover local development and the known deployments.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | the production and localhost origins | Comma-separated CORS allowlist |
| `ALLOWED_ORIGIN_REGEX` | empty (previews refused) | Extra origins by pattern. To let Vercel previews call the API, set it to `VERCEL_PREVIEW_ORIGIN_REGEX` from `app/config.py` |
| `LOG_LEVEL` | `INFO` | Root log level |
| `MAX_CONCURRENT_SIMULATIONS` | `1` | How many SWMM runs may execute at once |
| `MAX_QUEUED_SIMULATIONS` | `8` | Outstanding jobs allowed before new ones get `429` |
| `QUEUE_SLOTS_RESERVED` | `2` | The last places in the queue are kept for people who have started at most two runs in the past hour (`0` keeps none back) |
| `RESULT_RETENTION_SECONDS` | `900` | How long a finished result stays pollable |
| `MAX_RUNTIME_SECONDS` | `1800` | A run still going after this is failed, its queue slot freed, and the jobs behind it moved to a fresh worker |
| `MAX_QUEUE_WAIT_SECONDS` | `3600` | A job still waiting to start after this is failed |
| `SUPABASE_URL` | unset | The Supabase project the app signs people in with. **Required** unless `REQUIRE_AUTH=false` |
| `SUPABASE_ANON_KEY` | unset | That project's public (anon / publishable) key, sent when checking a token |
| `REQUIRE_AUTH` | `true` | Refuse simulations from callers who are not signed in. Turn off only for local development |
| `MAX_JOBS_PER_USER` | `1` | Runs one person may have queued or running at once |
| `MAX_RUNS_PER_USER_PER_HOUR` | `10` | Runs one person may start in an hour |
| `SUPABASE_SERVICE_ROLE_KEY` | unset | Records every run in the `simulation_runs` table so results survive a restart. A secret: set it only in the host's environment |
| `RUN_RETENTION_DAYS` | `7` | How long recorded runs are kept |
| `MAX_STORED_RUNS_PER_USER` | `20` | Recorded runs kept per person; a new run deletes their oldest finished ones beyond it (`0` keeps them all until they age out) |
| `REQUIRE_CONFIRMED_EMAIL` | `true` | Refuse accounts without a confirmed email address, anonymous sign-ins included (`403`). Turn off only if the app signs people in by phone |
| `MAX_JOBS_PER_IP` | `3` | Runs one client address may have queued or running at once, across all its accounts |
| `SUBMIT_RATE_LIMIT_PER_MINUTE` | `6` | `POST /simulations` requests per minute per client address (`0` turns it off) |
| `POLL_RATE_LIMIT_PER_MINUTE` | `120` | `GET /simulations/...` requests per minute per client address (`0` turns it off) |
| `TRUSTED_PROXY_HOPS` | `0` | How many proxies in front of the server append to `X-Forwarded-For`. **Set to `1` on Render** (see Deployment) |
| `MAX_REQUEST_BYTES` | `655360` | Largest request body accepted (`413` above it). The biggest real request, every node and link with every field, is about 495 KB |
| `ISOLATE_SIMULATIONS` | `true` | Run each simulation in a child process that is killed at `MAX_RUNTIME_SECONDS` |
| `ENABLE_DOCS` | `false` | Serve `/docs`, `/redoc` and `/openapi.json` |
| `ALLOW_INSECURE_DEPLOY` | `false` | Start on Render even with `REQUIRE_AUTH` off or `TRUSTED_PROXY_HOPS` at `0`, which otherwise stop the server starting there (see Deployment) |

A switch takes `true`/`false`, `yes`/`no`, `on`/`off` or `1`/`0`. Anything
else is an error at start-up rather than a guess.

Without `SUPABASE_URL` and `SUPABASE_ANON_KEY` the server refuses every
simulation with `503` rather than opening them to everyone. To try the API
locally without a Supabase project, set `REQUIRE_AUTH=false`.



## 🎯 How nodes are rated

Three numbers per node, kept separate so it is clear what each one knows.

**Hazard** — `Vulnerability_Score` (0–1) and `Vulnerability_Category`. How
badly the node floods: volume, duration as a share of the event, and peak
rate, each scaled against a fixed reference and weighted. It is monotonic —
more water, or longer, or faster can only raise it — and a node that floods
at all can never score zero.

**Exposure** — `Exposure_Score` (0–1), with `Barangay` and
`Population_Density`. Roughly how many people are around the node, from the
population density of the barangay it falls in.

**Risk** — `Risk_Score`, hazard × exposure. **Rank work lists on this.**
Hazard alone puts a drain in an empty lot level with one in the densest
barangay in the city.

> **What these are not.** The ratings come from simulation, not observation,
> and have not been checked against field records. Exposure is barangay
> density, not a count of who is inside the flood footprint — that would
> need building footprints and a routed inundation surface this project does
> not have. The reference values that set the hazard scale come from the
> 95th percentile of the shipped baseline run, not from a damage study, and
> are the first thing that should change once there is field evidence. Use
> `scripts/validate_against_reports.py` to check the ratings against citizen
> reports. What it would take to defend these ratings, rather than only
> describe them, is planned in [docs/SCIENCE_ROADMAP.md](docs/SCIENCE_ROADMAP.md).

Every result carries these limits in `metadata.model_info`: the date the
network was generated (from the `.inp` title), `calibrated: false`, the
hazard weights and reference values marked provisional, and what the model
leaves out (blocked drains, tide at the outfalls, wet ground, real storm
timing). The app shows them wherever the ratings are read.

Until 2026-09-29 results also carried `Legacy_Cluster_*` fields from the
k-means model this score replaced, for comparison. That model rated 272
nodes that flooded (one for 11.7 hours) as "No risk", and its top-50 work
list was identical to sorting on flood volume alone. Nothing read the
fields, so they were retired with the pickled model and scikit-learn. The
stored per-storm scenarios in the frontend's database still carry that
model's clusters until they are regenerated (docs/SCIENCE_ROADMAP.md, D).

The field names still say "Vulnerability" because they are the wire
contract. The user-facing term is "flood hazard".

## 🔌 API Endpoints

### Simulation Endpoints

A SWMM run takes roughly two minutes — longer than browsers and platform
proxies keep a request open — so simulations are **queued and polled**.

**Sign-in.** Both simulation endpoints need the caller's Supabase access
token: `Authorization: Bearer <access_token>`, the same token the app's
browser session holds. Missing, malformed or expired: `401`. A job can only
be read back by the person who started it; anyone else gets `404`. CORS is
not access control — it is a browser courtesy that `curl` ignores — so this
is what keeps the run queue for the app's users.

The server first checks what it can without a network call: the token is a
JWT for this project (`iss`), for a signed-in user (`aud` and `role` both
`authenticated`), not expired, with an allowed algorithm. A token signed
with one of the project's asymmetric keys (ES256/RS256) then has its
signature checked against the keys Supabase publishes, so a forged token
never reaches Supabase. Once the project publishes such keys, a token
claiming the legacy shared secret (HS256) is refused the same way; while
it publishes none, HS256 tokens go on to Supabase. Every token still
standing is then confirmed with Supabase Auth, which knows whether the
session is still live. Answers are cached
for a minute (refusals for 30 seconds). With `REQUIRE_CONFIRMED_EMAIL` on,
an account without a confirmed email gets `403`.

> **Signing out is not instant.** A token is accepted for up to a minute
> after its session is revoked, while its cached answer lasts.

**Limits.** Each client address may start 6 runs a minute and poll 120
times a minute (`429` past either), and hold 3 queued or running runs
across all its accounts. Each account may hold one, and start 10 an hour;
with `SUPABASE_SERVICE_ROLE_KEY` set, the hour is counted from the
`simulation_runs` table, so a restart doesn't reset it. An IPv6 caller is
counted as their /64 network, since they can send from any address in it.
The last 2 of the queue's 8 places are kept for accounts that have started
at most two runs in the past hour, so a few heavy users can't fill it
against everyone else. Request bodies over 640 KB get `413`.

- `POST /simulations` — queue a run. Returns `202` immediately with a job id
  and where to poll. Returns `429` when the caller already has a run going,
  has used their hourly allowance, their address is sending too much or
  already has too many runs waiting, or the server is full; `403` for an
  account without a confirmed email; `413` for a body over the limit; and
  `503` while the server is shutting down.
- `GET /simulations/{job_id}` — the job's state, and its result once it
  succeeds. Returns `404` once the result has expired. A finished result
  carries an `ETag`; send it back as `If-None-Match` and the answer is a
  bodiless `304`. (Browsers do this on their own.)
- `GET /health` — liveness probe: `{"status": "ok"}`.

`POST /simulations` accepts three optional sections:

```jsonc
{
  "nodes":    { "I-4": { "inv_elev": 16, "init_depth": 0 } },  // per-node overrides
  "links":    { "C-1": { "init_flow": 2.5 } },                 // per-conduit overrides
  "rainfall": { "total_precip": 400, "duration_hr": 24 }       // design storm, max 24 h
}
```

A request with none of them returns the pre-computed results for the
unmodified network, so it finishes almost immediately. Every such run
shares one copy of that result in memory, and its row in `simulation_runs`
stores `{"unmodified_network": true}` in place of the 0.7 MB result, which
the server fills back in when the run is read. Anything else runs a real
simulation, in a child process that is killed if it passes
`MAX_RUNTIME_SECONDS`.

Invalid input is rejected with `422` before anything is queued: every
value must be a finite number in a physically possible range (see
`app/schemas.py`), unknown fields are errors, and every node and link id
must exist in the network. SWMM used to skip an unknown id with a log line
and return the unmodified network as if the change had applied.

The flow:

```
POST /simulations            -> 202 { job_id, status: "queued", poll_url }
GET  /simulations/{job_id}   -> 200 { status: "running" }        # repeat
GET  /simulations/{job_id}   -> 200 { status: "succeeded", result: { ... } }
```

`result` carries `metadata` and `nodes_list`, one row per node. (It used to
repeat every row in a `nodes_dict` keyed by node ID, which doubled the size;
build a lookup on the client if you need one.) A failed run comes back as
`{ status: "failed", error: "..." }` with a `200` — the request to read the
job succeeded; the simulation is what failed.

**Runs survive restarts.** With `SUPABASE_SERVICE_ROLE_KEY` set, every run is
also written to the `simulation_runs` table in Supabase as it is queued,
starts and finishes (the table is defined in the frontend repository's
`supabase/schemas/schema_ops.sql`). A poll for a run the server no longer
holds in memory — it expired, or the server restarted — is answered from
there. On start-up, runs a restart cut short are marked failed with a
message saying so, and runs older than `RUN_RETENTION_DAYS` are deleted;
while the server stays up they are deleted once an hour, and each new run
trims its owner's finished runs to the newest `MAX_STORED_RUNS_PER_USER`.
Users can read their own runs from the table; nobody but the server can
write them. Without the key, runs live in memory only and are lost on a
restart; the server logs a warning at start-up.

> **Single worker.** Queued work still lives in the serving process, which
> is why the Procfile pins `--workers 1`, and start-up assumes any
> unfinished run in the table belonged to the previous process. Running
> several workers or instances needs a shared queue first.

---

## 📊 Data Processing

The `Python_Notebooks/` directory contains Google Colab notebooks for:

- **Data Preprocessing**: Converting raw drainage survey data into SWMM-compatible formats
- **Geospatial Processing**: Handling coordinate systems and network topology
- **Data Validation**: Ensuring data quality and completeness
- **Feature Engineering**: Inputs for the earlier k-means vulnerability model (retired)

### 📁 Raw Data Access

Raw datasets used for SWMM simulations are available at:

**[View Raw Data on Google Drive](https://drive.google.com/drive/folders/17EH76KdZrbVCcVJ79D_JurgRywqROQ8E?usp=drive_link)** 📂

---



## 🚀 Deployment

The backend is deployed on Render and serves the production API:

1. Connect your GitHub repository to Render
2. Configure environment variables (see Configuration above)
3. Render auto-deploys on every push to `main`

`Procfile` starts `app.main:app` and binds `$PORT`.

**The server refuses to start on Render** (where `RENDER` is set in the
environment) unless `TRUSTED_PROXY_HOPS` is at least `1` and `REQUIRE_AUTH`
is on. It logs what is wrong and exits, so the deploy fails instead of
serving open or with one rate limit shared by everyone. Setting
`ALLOW_INSECURE_DEPLOY=true` starts it anyway, with warnings.

Before the first deploy, set:

- `TRUSTED_PROXY_HOPS=1` (**required**). Render's proxy connects to the
  app, so without it every caller has the proxy's address and they all
  share one rate limit.
  The server reads the address the proxy appended to `X-Forwarded-For`, not
  the leftmost entry, which the caller writes. (Uvicorn's
  `--forwarded-allow-ips='*'` takes the leftmost, so it is not used.) If
  another proxy, such as a CDN, sits in front of Render, count it too. The
  server logs a warning if it sees `X-Forwarded-For` while this is `0`.
- `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY`.
- Leave `REQUIRE_AUTH` unset or `true` (**required**).
- Leave `ENABLE_DOCS` and `ALLOW_INSECURE_DEPLOY` unset.

---

## 📬 Contributing

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement".
Don't forget to give the project a star! Thanks again!

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

### 📢 Contributors

<a href="https://github.com/4Chronosx/drAIn/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=4Chronosx/drAIn" alt="contrib.rocks image" />
</a>

---

## ⚖️ License

This project is licensed under the GNU General Public License v2.0 (GPL-2.0).
You may redistribute and/or modify it under the terms of the GNU GPL, as published by the Free Software Foundation - see the [LICENSE](LICENSE) file for details.

---

## 🔗 Related Repositories

- [drAIn Frontend](https://github.com/eliseoalcaraz/drAIn) - Next.js web application

---

<p align="center">Made with 💧 for flood-resilient cities</p>

<!-- MARKDOWN LINKS & IMAGES -->
[contributors-shield]: https://img.shields.io/github/contributors/4Chronosx/drAIn.svg?style=for-the-badge
[contributors-url]: https://github.com/4Chronosx/drAIn/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/4Chronosx/drAIn.svg?style=for-the-badge
[forks-url]: https://github.com/4Chronosx/drAIn/network/members
[stars-shield]: https://img.shields.io/github/stars/4Chronosx/drAIn.svg?style=for-the-badge
[stars-url]: https://github.com/4Chronosx/drAIn/stargazers
[issues-shield]: https://img.shields.io/github/issues/4Chronosx/drAIn.svg?style=for-the-badge
[issues-url]: https://github.com/4Chronosx/drAIn/issues

