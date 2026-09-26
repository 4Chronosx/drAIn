# Science roadmap: from a defensible ranking to a defended one

*Status: 2026-09-26. Owner: whoever maintains `drAIn-backend`. This is a plan, not a record of what exists; section 6 lists what is already done.*

drAIn ranks drainage nodes by how badly a SWMM simulation says they flood (hazard), times how many people live around them (exposure). That design holds up: every number in it can be explained, and a ranking is what a maintenance crew needs. It has not yet been **defended**:
- the weights are chosen, not derived;
- exposure is coarse;
- the only check against reality is circular;
- the storms are idealised;
- the model cannot represent the thing the app exists to manage, which is a blocked drain.

This document sets out how to close each gap, in what order, and what "done" looks like.

---

## Summary

| # | Gap | Why it matters | First step (no new data) | Full fix | Effort |
|---|---|---|---|---|---|
| A | Hazard weights and reference values are chosen, not derived | The work list reshuffles under other weights, and nobody can say by how much | Sensitivity analysis: rank bands per node, top-50 stability | Set the weights from depth–damage data and observed events | S → L |
| B | Exposure is barangay density | Inside a barangay the ranking reverts to hazard alone, which is where the "fix this first" decisions happen | Stop scoring unmapped nodes at a made-up 0.5 | Gridded population, then buildings, then people inside a routed flood footprint | M → XL |
| C | Validation against reports is circular | Reports come from where people live, and exposure rewards the same thing | Validate hazard, not risk, within barangays, controlling for population | Hindcast documented storms against independent flood records | S → L |
| D | Storms are triangles; no tide, no wet ground | Peak timing drives peak flooding; high tide during heavy rain is a real Mandaue flood mechanism | Rainfall shapes from PAGASA RIDF curves | Tidal outfalls, surge scenarios, antecedent moisture, observed storms | M |
| E | A clogged drain can't be simulated | The maintenance loop and the simulation loop never meet | A bounded "percent blocked" override per conduit | Condition-aware runs fed by maintenance records; value of each cleaning | M → L |
| F | "AI-powered" claims more than the code does | Credibility with engineers and reviewers | Say what the system is | Add machine learning only where it earns its place, validated | S |
| G | One city, one hand-built model, frozen | Users can't tell how old or how trustworthy the network is | Version-stamp every result | A repeatable rebuild-and-regress process when the GIS changes | S → M |
| H | The UI states no uncertainty | A "No hazard" node that floods is a liability | A caveat on every ratings table (**done**) | Rank bands, evidence conflicts, per-node confidence | S → M |

Effort: S ≈ days, M ≈ 1–3 weeks, L ≈ 1–2 months, XL ≈ a season or a partner.

---

## 1. The model as it stands

Every number below comes from the shipped files in this repository.

**Network.** The network file `data/Mandaue_Drainage_Network.inp` was generated from GIS data on 2025-11-18 by `Python_Notebooks/INP_FILE_GENERATOR.ipynb`.
- 1,413 nodes: 1,369 junctions and 44 outfalls.
- 1,428 conduits and 1,369 subcatchments, covering 3,181 ha.

**Subcatchments.**
- They are Voronoi (Thiessen) polygons around the nodes, not catchments delineated from terrain.
- Imperviousness is inferred from a Manning's-n raster with a linear formula: `(0.045 − n) / 0.03 × 100`, falling back to 35%. The mean is 62%.
- Infiltration is Horton with textbook parameters (for example 100 / 15 mm/h, decay 4).

**Pipes and nodes.** Much of the geometry is defaults:
- 655 of 1,428 pipes are 0.6 m, the generator's fallback diameter.
- All 1,428 pipes are circular, with Manning's n of 0.013.
- Every node is 1.2 m deep, the default.
- Every node's ponded area is 0. With `ALLOW_PONDING YES` and no ponded area, water that overflows a node leaves the model. It does not pond, return or spread to neighbouring streets. So "flood volume" is water lost from the pipes, not a depth on the street.

**Outfalls.** All 44 outfalls are `FREE`: the sea never backs water up.

**Baseline storm.** The shipped series `TS_Rain` holds about 266 mm over 24 hours, in 5-minute steps, peaking at 22 mm/h. User-defined storms are a symmetric triangle (`drain/rainfall.py`).

**Hazard** (`drain/hazard.py`).
- Weights: 0.5 × volume, 0.3 × hours flooded as a share of the storm, 0.2 × peak overflow rate.
- Full scale is 45 ML and 1.0 m³/s, both the 95th percentile of the baseline run.
- Categories: High above 0.5, Medium above 0.25, Low above 0.
- Baseline result: 963 nodes No hazard, 294 Low, 108 Medium, 48 High.

**Exposure** (`drain/exposure.py`).
- The score is the density of the barangay a node falls in, divided by the densest barangay's.
- A node outside every mapped barangay scores 0.5.

**Stored scenarios.** The `flood_results` table behind the simulation page's per-return-period tables was rated by the superseded k-means model: exactly four distinct scores, one per cluster. Live runs use the hazard score, so the two tables can't be compared node for node.

**Consistency.** In the baseline, 89 nodes are listed as flooded in the `.rpt` summary but show no overflow in the `.out` time series (the payload's `inconsistent_nodes`).

**Validation.** `scripts/validate_against_reports.py` compares hazard categories with the components citizens report. Until this branch it could not run: it looked up "No risk" where the scorer emits "No hazard". It also read only the first 1,000 reports.

**API semantics.** The API's link override `init_flow` sets SWMM's `flow_limit`, not initial flow (`drain/swmm_runner.py`, `LINK_FIELDS`). The field should be renamed on the wire, or the UI label changed, before anyone draws conclusions from it.

What this means for the plan: the largest uncertainties are not in the scoring formula. They are in the network (pipe sizes, depths, catchments) and in what the model leaves out (surface flow, tide, blockage). A perfectly weighted score on a network half made of defaults is still a guess. Workstreams A, C and G make that visible; B, D and E reduce it.

---

## 2. Workstreams

### A. Defend the hazard weights

**Problem.** The weights (0.5 / 0.3 / 0.2) and the full-scale references (45 ML, 1 m³/s) are reasoned, not derived. A reviewer's first question is how much the work list moves under other choices, and nothing answers it.

**First step: measure the sensitivity.** No new data is needed.
1. Add `scripts/sensitivity.py`. Draw N = 2,000 weight vectors from a Dirichlet distribution centred on (0.5, 0.3, 0.2), for example α = 20 × the weights. Draw the full-scale references log-uniformly between half and double their values.
2. Rescore the baseline and each stored return period with every draw. For each node, record the 5th, median and 95th percentile of its rank.
3. Report per scenario:
   - Kendall's τ against the current ranking;
   - Jaccard overlap of the top 50 against the current top 50;
   - the share of today's top 50 that stays in the top 50 in at least 90% of draws.
4. Add two reference rankings, to show whether the composite adds anything: volume alone, and hours flooded alone.
5. Publish `docs/sensitivity-<date>.md`, and send each node's rank band with the results as `Rank_Band_P5` and `Rank_Band_P95`.

*Acceptance:* the README states the top-50 stability number. A node's rank band is visible in the results table.

**Full fix: ground the weights in consequences.**
- Replace the volume term with an estimated flood depth once B's surface routing exists, or with ponded depth once D and E give nodes real ponded areas. Map depth to damage with depth–damage curves: the JRC global curves (Huizinga et al., 2017) have Asian residential and commercial curves.
- Replace the duration term's reference with traffic and access disruption thresholds agreed with the City Engineering Office, for example the depth at which a road closes to light vehicles.
- Fit the remaining weights to observed events (see C). Choose the weights that best rank the nodes where flooding was recorded, and check them on a held-out event.

*Risks:* too few observed events to fit anything. Mitigation: keep the transparent formula and publish its sensitivity rather than overfitting.

---

### B. Exposure: from barangay density to people near the water

**Problem.** Every node in a barangay gets the same multiplier, so within a barangay the risk ranking is just the hazard ranking. The fallback of 0.5 for unmapped nodes is invented, and can outrank a genuinely sparse area.

**Step 1 (S): stop inventing exposure.**
- For a node outside every polygon, use the nearest barangay within 250 m. Past that, return `Exposure_Score: null`, and rank those nodes in a separate "exposure unknown" group instead of scoring them 0.5.
- Report how many nodes fall back, per run.

**Step 2 (M): gridded population.**
- Use WorldPop's constrained 100 m population grid for the Philippines, or Meta's high-resolution settlement layer (30 m). Check both against the 2020 census totals per barangay (PSA), which are already in `data/mandaue_population.geojson`.
- Exposure = people inside the node's subcatchment polygon. The polygons are already in the `.inp` `[POLYGONS]` section. Also compute people within a fixed radius, for comparison.
- Normalise against a fixed reference (for example the 95th percentile), not the densest barangay, so adding a barangay doesn't rescale everyone.

**Step 3 (M): buildings and critical facilities.**
- Count building footprints per subcatchment. Microsoft's global ML building footprints and OpenStreetMap both cover Mandaue; Google Open Buildings covers Southeast Asia.
- Add a critical-facility term for hospitals, schools, evacuation centres and fire stations (OSM, checked with the city). Keep it as a separate column, not folded silently into the score.

**Step 4 (XL): people inside the flood.**
- This needs a routed inundation surface: water leaving a node has to spread over the DEM. Options:
  - a 1D–2D coupled model, SWMM with a 2D surface mesh (as PCSWMM does, or by coupling to HEC-RAS 2D or LISFLOOD-FP);
  - or a cheaper "fill and spill" over the DEM from each node's overflow volume.
- Exposure = people and buildings where depth exceeds 0.15 m.
- This is the step that turns "flood volume" into "who gets wet". It is also a research project; scope it with a university partner.

*Acceptance for steps 1–2:*
- no node scored on an invented number;
- within-barangay rank differences exist and are explained in the README;
- exposure stable when the barangay layer changes.

---

### C. Validation that isn't circular

**Problem.** Citizen reports are densest where people live and use the app. The exposure weighting rewards population too, so "risk agrees with reports" partly measures population twice. Reports are also unverified observations (now partly addressed, see section 6).

**Step 1 (S): validate the right thing, the right way, with today's data.**
- Validate **hazard**, not risk. Hazard contains no population, so agreement is not built in.
- Stratify: compare reported against unreported nodes **within the same barangay**. Report the within-barangay AUC and the share of barangays where reported nodes rank higher.
- Model report counts per node with a negative binomial regression: `reports ~ hazard + log(population around node) + barangay effects`. The hazard coefficient, with its interval, is the evidence. A model that only predicts population will show a hazard coefficient near zero.
- Use only trustworthy reports: `review_status = 'confirmed'`, or `photo_check = 'match'`. Count distinct reporters per component, not reports. Those fields exist now.
- Add these to `scripts/validate_against_reports.py` next to the current top-N table, and state the sample size. With a handful of reports the answer is "insufficient data", and the script should say so rather than print a verdict.

**Step 2 (L): independent ground truth.**
- Collect flood observations that don't come from the app:
  - the Mandaue City DRRM office's incident logs;
  - DPWH and city flood-prone area maps;
  - geocoded news reports;
  - flood extents from Sentinel-1 radar imagery for named events (radar sees through cloud; Typhoon Odette/Rai, December 2021, is one candidate).
- **Hindcast**:
  1. Get the event's observed rainfall (PAGASA hourly data, Mactan station).
  2. Run SWMM with it, with the tide at that time once D exists.
  3. Score the predicted flooded nodes against the observed extents: hit rate, false-alarm rate, AUC, and precision within the top-k work list.
- Hold out at least one event for testing if the weights are later fitted (see A).

*Acceptance:* a published validation note with its sample sizes, and a within-barangay or hindcast AUC with a confidence interval. The README's "has not been checked against field records" is replaced by what was checked, and how well it did.

---

### D. Storms that behave like storms, plus tide and wet ground

**Problem.**
- The design storm is a symmetric triangle. Real hyetographs are front- or centre-loaded, and peak timing drives peak flooding.
- Ground always starts dry.
- The 44 outfalls discharge freely. In coastal Mandaue, high tide or storm surge during heavy rain backs water up the system, and the model can't express that.

**Step 1 (M): design storms from local data.**
- Build return-period storms from PAGASA's rainfall intensity–duration–frequency (RIDF) table for the nearest station (Mactan), using the alternating block method. Offer Huff-quartile shapes as an option.
- Let `POST /simulations` accept an **observed hyetograph** (a list of time and intensity pairs, bounded like the other inputs) for hindcasts.
- Regenerate the stored `flood_results` scenarios from these storms, scored with the current hazard score. This also retires the k-means ratings the stored tables still show (see F).
- Lift the 24-hour cap for observed multi-day typhoon rain, by setting END_DATE from the storm's length.

**Step 2 (M): tide and surge.**
- First, confirm the network's elevation datum against mean sea level. If node elevations are not tied to MSL, a tide level means nothing. Check a few surveyed benchmarks against the DEM the notebook used.
- Switch the outfalls to `TIDAL`, using a tidal curve from NAMRIA tide predictions for Cebu or the Mactan Channel, or to `FIXED` at chosen stages. Add a surge offset (for example +0.5, +1.0, +1.5 m) for typhoon scenarios.
- Run a **coincidence matrix**: rain return period × tide level. Report which nodes flood only when the two coincide. That list is what the city can't see today.
- Add check valves (`Gated YES`) where outfalls have flap gates. The GIS outlets layer has a `flapgate` field; carry it through the generator.

**Step 3 (S–M): antecedent conditions.**
- Offer dry, normal and wet starting states. Either set the Horton parameters' initial state through a SWMM hot-start file saved after a spin-up period of prior rain, or run a 3–5 day antecedent series before the design storm.

*Acceptance:*
- stored scenarios regenerated from RIDF storms, with the hazard score;
- a tide-coincidence result for at least the 10- and 50-year storms;
- the storm shape and the tide assumption shown in each result's `model_info`.

---

### E. Simulate a clogged drain, and let maintenance and simulation meet

**Problem.** The app's purpose is proactive maintenance, but the model has every pipe clean. It can't show what a blocked drain costs, or what clearing it buys. So a maintenance record never changes a simulation, and a simulation never ranks a cleaning.

**Step 1 (M): a blockage input.**
- Add `blockage: { "<link id>": fraction }` to `POST /simulations`, bounded 0 to 0.95 like the other overrides. Model it by:
  - reducing the conduit's flow area: set an equivalent smaller circular diameter through `[XSECTIONS]` in the pre-config (pyswmm does not appear to expose cross-section size while a run is going; check before building on it);
  - optionally raising Manning's n for silted pipes.
- Represent blocked inlets at nodes by limiting inflow. SWMM 5.2's street and inlet objects (`[STREETS]`, `[INLETS]`, `[INLET_USAGE]`) are the proper tool. The current network models inlets as plain junctions, so this is a generator change.
- Give nodes a real ponded area (from the subcatchment or the DEM), so a blocked drain produces standing water that drains back when it can, instead of water deleted from the model.

**Step 2 (L): condition-aware runs.**
- Derive a condition per component from the database: the latest maintenance (`maintenance`, with `verification_status`) and confirmed citizen reports (`reports`, with `review_status = 'confirmed'`).
- Model silting as a function of time since the last verified cleaning: for example X% of area lost per month, calibrated from inspection notes. Start with a conservative assumption, and label it as such.
- Run "as maintained" against "all clean" for each storm. For each component, the difference in flood volume at the nodes it serves, weighted by exposure, is **the risk reduction from cleaning it**.
- Rank cleanings by risk reduction per unit of effort. This is the maintenance work list the app has always promised.

**Step 3 (M): close the loop in the UI.**
- After staff record a cleaning, show "Clearing C-88 reduces simulated flooding at I-4 by about N ML in a 10-year storm (model estimate)".
- When citizens keep reporting a node the model rates "No hazard", flag it as **model and reports disagree**, as a prompt to inspect the drain and to fix the network data.

*Acceptance:*
- a run with `C-88` 80% blocked floods more than the clean run, at the nodes downstream of it;
- a published "value of cleaning" list for the 10-year storm;
- the maintenance tab shows the estimate, labelled as a model estimate.

---

### F. Say what the system is

**Problem.** The name and the README say "AI-powered", and the assistant's prompt mentions satellite data. In fact:
- the k-means model is superseded;
- the hazard score is a weighted average, correctly so;
- Gemini answers questions about the docs.

Overclaiming costs credibility with exactly the engineers the tool is for.

**Step 1 (S): accurate claims.**
- Describe the system as a physics-based drainage simulation (EPA SWMM) with transparent hazard and exposure scoring, and an AI assistant for questions about the documentation.
- Audit these places:
  - the frontend landing and docs pages (`app/(main)/docs/sections/*`);
  - the README badges and "Simulation & ML" section;
  - `lib/chatbot/prompts.ts`: the "satellite data" and "AI analysis" lines. The assistant is now at least told that ratings are simulated and provisional.
- Retire the `Legacy_Cluster_*` fields and the pickled k-means model once D regenerates the stored scenarios. Keep the comparison in a dated note.

**Step 2 (L, optional): machine learning where it earns its place.** Each of these needs its own validation before it ships.
- **A surrogate model** trained on many SWMM runs, answering what-if questions in milliseconds instead of minutes, with a stated error against SWMM on held-out runs. The notebook's Drive folder is named "ML Surrogate Model", so this was the original intent.
- **Photo triage for reports:** a small image classifier flagging "not a drain" and "blocked drain" photos, to help staff review. It is advisory only; staff decide.
- **Learning from confirmed reports and maintenance records** to calibrate the silting rate in E.

*Acceptance:* no claim in the UI or docs that the code doesn't back.

---

### G. Keep the model current, and say how current it is

**Problem.** One hand-built network with no version. Nothing in the UI said when it was built (fixed now), and there is no process for rebuilding it when the city changes.

1. **Version stamp (S).**
   - Hash the `.inp` and put the hash in `model_info` with the build date.
   - Store both with every `simulation_runs` row and every `flood_results` row.
   - Show "network model version and date" on results, and changes between versions in a changelog.
2. **Rebuild and regress (M).**
   - Make the generator notebook a script with pinned inputs (DEM, GIS layers, the Manning raster) and a record of where each came from.
   - On every rebuild, run the baseline and the stored storms, and diff them: flooded-node count, top-50 overlap, category shifts. Publish only after review.
3. **Replace defaults with data where it matters most (M, ongoing).**
   - Of 1,428 pipes, 655 carry the 0.6 m fallback diameter; every node is 1.2 m deep; every pipe is circular, with n = 0.013.
   - Use the sensitivity results (A) to find which defaults move the top of the work list most. Ask the City Engineering Office for as-built plans or survey data for those first. That is a value-of-information order, not a street-by-street survey.
   - Represent open channels and box culverts with their real shapes.
4. **Investigate the 89 nodes** whose `.rpt` and `.out` disagree about flooding (S). They are either a reporting-interval artefact or a real inconsistency, and until known their time-to-overflow is unusable.

---

### H. Uncertainty in the UI

**Done:** every ratings table opens with a notice. It says:
- the ratings are simulated, not observed;
- the network model's date, and that it has not been checked against field records;
- the thresholds are provisional;
- "No hazard" does not mean safe;
- stored scenarios come from an earlier model;
- what the model leaves out.

Live results carry `metadata.model_info`, built from the scorer's own constants.

**Next:**
- rank bands from A, shown as "ranks 12–40 across plausible weightings";
- the evidence-conflict flag from E3;
- a per-node "data quality" note for nodes on default diameter or depth (from G3);
- show the storm shape and the tide assumption with each run once D lands.

---

## 3. Order of work

**Phase 1, weeks 1–3. Make the uncertainty visible (no new data).**
- A1: sensitivity and rank bands.
- C1: within-barangay validation of hazard, with confirmed reports only.
- B1: no invented exposure.
- G1: version stamp.
- G4: investigate the 89 inconsistent nodes.
- F1: accurate claims.
- Rename or relabel `init_flow` (it sets the flow limit).

**Phase 2, weeks 4–10. Better storms and exposure.**
- D1: RIDF storms, observed hyetographs, and regenerating the stored scenarios with the hazard score, which retires the k-means ratings.
- D2: first check the datum, then tidal outfalls and the coincidence matrix.
- B2: gridded population by subcatchment.
- E1: blockage input and ponded areas.

**Phase 3, months 3–4. Close the loops.**
- E2 and E3: condition-aware runs and the value of each cleaning.
- C2: hindcast of at least one documented event against independent observations.
- A (full fix): weights fitted to that evidence, if it supports fitting.
- G3: targeted survey of the defaults that matter most.

**Phase 4, with a partner.**
- B4: 2D inundation and people in the footprint.
- F2: a surrogate model.
- A second city: a new network, reusing the pipeline.

Dependencies:
- C2 needs D1 for observed storms, and ideally D2 for tide.
- A's full fix needs C2.
- E2 needs E1, and verified maintenance records (section 6).
- B4 needs the ponded areas from E1, plus a DEM check.

---

## 4. Data to request

| Data | From | Used by |
|---|---|---|
| RIDF table and hourly rainfall, Mactan station | PAGASA | D1, C2 |
| Tide predictions and datum, Cebu / Mactan Channel | NAMRIA | D2 |
| Flood incident logs with locations and dates | Mandaue City DRRM office | C2 |
| As-built drainage plans; inspection and cleaning records | City Engineering Office, DPWH | G3, E2 |
| Flap gate and outfall inventory | City Engineering Office | D2 |
| Gridded population | WorldPop or Meta (open) | B2 |
| Building footprints, critical facilities | OSM, Microsoft, Google (open) | B3 |
| Radar flood extents for named events | Copernicus Sentinel-1 (open) | C2 |
| The DEM and Manning raster the network was built from | Project Drive (`ML Surrogate Model/`) | G2, D2, B4 |

---

## 5. When the ranking counts as defended

1. **Stability is published:** the share of the top 50 that survives plausible reweighting, with rank bands in the UI.
2. **Validation is not circular:** a within-barangay or hindcast result with its sample size and confidence interval, using only confirmed observations.
3. **The model can express the city's real mechanisms:** tide coincidence and blocked drains.
4. **Every result says:**
   - which network version it came from;
   - which storm, tide and antecedent assumptions it used;
   - how exposure was measured;
   - that it is a simulation.
5. **The claims match the code.**

---

## 6. Already done (branch `trust-and-ops`)

- **Report data is fit to validate against.** Staff can confirm or reject reports.
  - Rejected reports drop out of every count and out of the validation script.
  - Each reporter can have one open report per component, and reports per hour are capped.
  - Each report records where its photo's GPS puts it relative to the component (`photo_check`).
- **Fixes are checked independently.** Resolved maintenance is `unverified` until a colleague or the reporter confirms it; a dispute reopens the reports. E2 can trust "last verified cleaning" as an input.
- **The results say what they can't claim.** `metadata.model_info` in every result, and a notice on every ratings table.
- **The validation script runs.** It ranks "No hazard", reads every report a page at a time, and skips rejected reports.
- **The simulation API is fit for experiments.**
  - Runs need sign-in and have per-user limits.
  - Inputs are bounded, and unknown node and link ids are rejected; SWMM used to skip them silently.
  - Runs are stored in Supabase for 7 days, so a batch of hindcast runs survives a restart.

## 7. Open questions

- Is the network's elevation datum mean sea level? D2 depends on the answer.
- Which surveyed data does the City Engineering Office hold, and in what form?
- Will the city share incident logs? If not, C2 relies on radar imagery and news reports, and its resolution suffers.
- Who owns the model rebuild once the original authors move on? G2 needs a named maintainer.
