import json
from fastapi import FastAPI # type: ignore
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from tools.swmm_extract import get_node_flooding_summary_with_vulnerability
from tools.swmm_tools import simulate_new

app = FastAPI()

# Allow local frontend + production Vercel domains
origins = [
    "http://localhost:3000",
    "https://pjdsc-drain.vercel.app",
    "https://project-drain.vercel.app",
    "https://ai-drain.vercel.app"
]

# Vercel gives every preview deploy a unique hostname, so they can't be listed
# above. Match them by pattern instead of using "*", which is rejected by
# browsers when allow_credentials=True.
origin_regex = r"https://(pjdsc-drain|project-drain|ai-drain|drain)-[a-z0-9-]+\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SimulationRequest(BaseModel):
    nodes: dict
    links: dict
    rainfall: dict

@app.post("/run-simulation")
def run_simulation(request: SimulationRequest):
    nodes = request.nodes
    links = request.links
    rainfall = request.rainfall
    try:
        simulate_new('data/Mandaue_Drainage_Network.inp', nodes, links, rainfall)
    except Exception as e:
        print(f"Unexpected error occured: {e}")

    jsonData = None
    data = {}
    try:
        if rainfall:
            jsonData = get_node_flooding_summary_with_vulnerability('data/Mandaue_Drainage_Network_mod.rpt','data/Mandaue_Drainage_Network_mod.out')
        else:
            jsonData = get_node_flooding_summary_with_vulnerability('data/Mandaue_Drainage_Network.rpt', 'data/Mandaue_Drainage_Network.out')
        data = json.loads(jsonData)
    except Exception as e:
        print(f"Unexpected error occured: {e}")
    finally:
        return data

