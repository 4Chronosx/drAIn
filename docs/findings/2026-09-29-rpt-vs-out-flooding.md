# Why 89 nodes flood in the report but not in the output file

**Question (roadmap G4).** On the baseline run, 89 nodes appear in the
`.rpt` Node Flooding Summary with hours flooded above zero, yet their
`FLOODING_LOSSES` series in the `.out` file never goes above zero. The API
counts them in `metadata.inconsistent_nodes` and serves their
`Time_After_Raining_min` as null. Is it our parsing?

**Answer: no.** It is how SWMM reports, and it doesn't affect the ratings.

## What the two files measure

- The `.rpt` summary is accumulated by SWMM at **every routing step**:
  `ROUTING_STEP 0:00:10` with `VARIABLE_STEP 0.75`, so every 10 seconds or
  less. Any step with overflow adds to "hours flooded".
- The `.out` file stores node values only at **reporting times**:
  `REPORT_STEP 00:01:00`, once a minute, interpolated between the routing
  steps either side.

A flood that starts and stops between two whole minutes, or that flickers on
and off, is in the first and invisible in the second.

## The 89 nodes

Checked on the shipped baseline (`data/Mandaue_Drainage_Network.rpt/.out`):

| Hours flooded (from `.rpt`) | Nodes | Explanation |
|---|---|---|
| ≤ 1 minute (most are 0.01 h, 36 s) | 64 | Shorter than the reporting step. |
| 1–5 minutes | 15 | Short, and see below. |
| over 5 minutes (up to 0.8 h) | 10 | Water at the rim, flickering. |

The longer ones share a pattern. Take ISD-118: 0.8 h flooded, peak 0.001 m³/s
(1 L/s), volume 0.000 × 10⁶ L, maximum depth 1.198 m, which is its full
depth. The node sits at the rim, and overflow switches on and off from one
routing step to the next. The interpolated value at each whole minute lands
on zero. ISD-411 (0.35 h, 1 L/s) and ISD-384 (0.27 h, depth 1.200 m) look
the same.

`ALLOW_PONDING YES` is not the cause: none of them hold ponded water.

## Does it matter?

- All 89 rate **Low** (highest hazard score 0.074).
- Together they spill 111 m³, **0.0026%** of the network's total flood
  volume.
- Only `Time_After_Raining_min` is affected, because it is the one figure
  taken from the `.out` series. Hazard uses the `.rpt` figures.

## What we do

- Keep `Time_After_Raining_min` null for them, as now: the `.out` file has
  no onset to report, and inventing one would be worse.
- Keep counting them in `metadata.inconsistent_nodes`.
- If onset times for brief floods ever matter, set `REPORT_STEP` to the
  routing step (10 s). The `.out` file grows about sixfold, and the network
  file changes, which roadmap G2 (rebuilding the `.inp`) should decide.
