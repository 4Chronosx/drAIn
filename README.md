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
  <a href="https://github.com/4Chronosx/BACKEND-DrAin">
    <img src="logo.png" alt="drAIn Backend logo" width="40%" height="35%">
  </a>
  <br />
  <p align="center">
    <a href="#"><img alt="Status" src="https://img.shields.io/badge/status-Beta-yellow?style=flat&color=yellow" /></a>
    <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white&style=flat" /></a>
    <a href="https://github.com/4Chronosx/BACKEND-DrAin/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/4Chronosx/BACKEND-DrAin?color=coral&logo=git&logoColor=white" /></a>
  </p>
  <a href="https://github.com/4Chronosx/BACKEND-DrAin/issues/new?labels=bug&template=bug-report---.md">Report Bug</a>
  &middot;
  <a href="https://github.com/4Chronosx/BACKEND-DrAin/issues/new?labels=enhancement&template=feature-request---.md">Request Feature</a>
</div>

---

## 🗺️ Overview

The **drAIn Backend** powers the simulation engine and API infrastructure for the drAIn platform. Built with **FastAPI** and **PySWMM**, it provides RESTful endpoints for running **SWMM (Storm Water Management Model)** hydraulic simulations, processing drainage system data, and delivering real-time flood hazard analytics to the frontend.

This backend transforms complex hydrological modeling into accessible API services, enabling engineers and planners to run sophisticated urban drainage simulations through simple HTTP requests.

---

### 💡 Why This Backend?

Urban flood modeling typically requires specialized software and technical expertise. The drAIn backend democratizes access to SWMM simulations by:

* **Abstracting Complexity**: Wraps SWMM's Python API in intuitive REST endpoints
* **Enabling Real-Time Simulation**: Supports interactive "what-if" scenarios for infrastructure planning
* **Processing at Scale**: Handles data preprocessing and result analysis automatically
* **Cloud-Ready Architecture**: Deployed on Render for reliable, scalable API access

> **Scope.** This serves one hand-built SWMM model of Mandaue. The API is
> the cheap part; the expensive part is the calibrated network behind it —
> invert levels, pipe geometry, catchment delineation, survey work — which
> is months of engineering per city. Another city needs that model built
> first, not just a redeploy.

#### ⚙️ Core Capabilities

* 🌊 **SWMM Simulation Engine**: Python-based hydraulic and hydrological modeling using PySWMM
* 🚀 **High-Performance API**: FastAPI endpoints optimized for concurrent simulation requests
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
  <a href="https://scikit-learn.org/"><img alt="scikit-learn" src="https://img.shields.io/badge/scikit--learn-F7931E?logo=scikitlearn&logoColor=white&style=flat" /></a>
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
│   ├── config.py          # Settings read from the environment
│   ├── logging_config.py  # Logging setup
│   ├── main.py            # App factory, CORS, routes
│   └── schemas.py         # Request/response models
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
│   └── vulnerability.py   # The superseded k-means model
├── scripts/               # Analysis, not imported by the server
│   └── validate_against_reports.py
├── data/                  # SWMM network and trained model
│   ├── Mandaue_Drainage_Network.inp
│   ├── Mandaue_Drainage_Network.out
│   ├── Mandaue_Drainage_Network.rpt
│   ├── mandaue_population.geojson
│   └── vulnerability_model_k4.pkl
├── tests/                 # pytest suite
├── Python_Notebooks/      # Data preprocessing notebooks
│   ├── INP_FILE_GENERATOR.ipynb
│   └── KMEANS_MODEL.ipynb
├── Procfile               # Process configuration
├── pyproject.toml         # ruff and pytest configuration
├── requirements.txt       # Pinned runtime dependencies
└── requirements-dev.txt   # Adds pytest and ruff
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
git clone https://github.com/4Chronosx/BACKEND-DrAin.git
cd BACKEND-DrAin

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# ...or, to also get pytest and ruff
pip install -r requirements-dev.txt
```

---

### 🚀 Running the API

```bash
# Start the FastAPI server
uvicorn app.main:app --reload

# Server will be available at http://localhost:8000
# Interactive API docs at http://localhost:8000/docs
```

To run a simulation without starting the server:

```bash
python -m drain.cli --precip 400 --duration 24 --node I-4
```

### ✅ Tests and linting

```bash
pytest
ruff check drain app tests
ruff format --check drain app tests
```

### 🔧 Configuration

All optional; the defaults cover local development and the known deployments.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | the production and localhost origins | Comma-separated CORS allowlist |
| `ALLOWED_ORIGIN_REGEX` | Vercel preview pattern | Matches per-deploy preview hostnames |
| `LOG_LEVEL` | `INFO` | Root log level |
| `MAX_CONCURRENT_SIMULATIONS` | `1` | How many SWMM runs may execute at once |
| `MAX_QUEUED_SIMULATIONS` | `8` | Outstanding jobs allowed before new ones get `429` |
| `RESULT_RETENTION_SECONDS` | `900` | How long a finished result stays pollable |
| `MAX_RUNTIME_SECONDS` | `1800` | A run still going after this is failed and its queue slot freed |



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
> reports.

`Legacy_Cluster_Category` and `Legacy_Cluster_Score` carry the previous
k-means output, kept so the two can be compared. It rated 272 nodes that
flooded — one for 11.7 hours — as "No risk", and its top-50 work list was
identical to sorting on flood volume alone.

The field names still say "Vulnerability" because they are the wire
contract. The user-facing term is "flood hazard".

## 🔌 API Endpoints

### Simulation Endpoints

A SWMM run takes roughly two minutes — longer than browsers and platform
proxies keep a request open — so simulations are **queued and polled**.

- `POST /simulations` — queue a run. Returns `202` immediately with a job id
  and where to poll. Returns `429` when too much work is already outstanding.
- `GET /simulations/{job_id}` — the job's state, and its result once it
  succeeds. Returns `404` once the result has expired.
- `GET /health` — liveness probe; also reports whether hazard scoring
  is available.
- `POST /run-simulation` — **deprecated.** Runs the simulation and waits for
  it, holding the request open for the whole run. Kept only so a frontend
  deployed before the queued endpoints keeps working; remove it once no
  deployed client calls it.

`POST /simulations` accepts three optional sections:

```jsonc
{
  "nodes":    { "I-4": { "inv_elev": 16, "init_depth": 0 } },  // per-node overrides
  "links":    { "C-1": { "init_flow": 2.5 } },                 // per-conduit overrides
  "rainfall": { "total_precip": 400, "duration_hr": 24 }       // design storm, max 24 h
}
```

A request with none of them returns the pre-computed results for the
unmodified network, so it finishes almost immediately. Anything else runs a
real simulation.

Invalid input is rejected with `422` before anything is queued.

The flow:

```
POST /simulations            -> 202 { job_id, status: "queued", poll_url }
GET  /simulations/{job_id}   -> 200 { status: "running" }        # repeat
GET  /simulations/{job_id}   -> 200 { status: "succeeded", result: { ... } }
```

`result` carries `metadata`, `nodes_list` (for iteration) and `nodes_dict`
(for lookup by node ID). A failed run comes back as
`{ status: "failed", error: "..." }` with a `200` — the request to read the
job succeeded; the simulation is what failed.

> **Single worker.** Jobs live in the serving process's memory, which is why
> the Procfile pins `--workers 1`. With more than one, a poll can land on a
> process that has never heard of the job. Running several workers, or more
> than one instance, needs a shared job store (Redis, or a table) first.

---

## 📊 Data Processing

The `Python_Notebooks/` directory contains Google Colab notebooks for:

- **Data Preprocessing**: Converting raw drainage survey data into SWMM-compatible formats
- **Geospatial Processing**: Handling coordinate systems and network topology
- **Data Validation**: Ensuring data quality and completeness
- **Feature Engineering**: Creating inputs for ML-based vulnerability ranking

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

<a href="https://github.com/4Chronosx/BACKEND-DrAin/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=4Chronosx/BACKEND-DrAin" alt="contrib.rocks image" />
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
[contributors-shield]: https://img.shields.io/github/contributors/4Chronosx/BACKEND-DrAin.svg?style=for-the-badge
[contributors-url]: https://github.com/4Chronosx/BACKEND-DrAin/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/4Chronosx/BACKEND-DrAin.svg?style=for-the-badge
[forks-url]: https://github.com/4Chronosx/BACKEND-DrAin/network/members
[stars-shield]: https://img.shields.io/github/stars/4Chronosx/BACKEND-DrAin.svg?style=for-the-badge
[stars-url]: https://github.com/4Chronosx/BACKEND-DrAin/stargazers
[issues-shield]: https://img.shields.io/github/issues/4Chronosx/BACKEND-DrAin.svg?style=for-the-badge
[issues-url]: https://github.com/4Chronosx/BACKEND-DrAin/issues

