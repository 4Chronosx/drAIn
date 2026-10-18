# Mandaue flood history: rain thresholds and a first independent model check

**Question (roadmap C, step 2).** Google's Groundsource dataset holds 357
news-derived flood records inside Mandaue, 2011–2025. Paired with hourly
rain, how much rain comes with a reported flood? And do the barangays that
flood most often in the news hold the nodes the model rates worst?

**Answer, in short.** Rain separates flood days from other days only
moderately (AUC about 0.75 for the 24-hour total), and no single threshold
is sharp: heavy rain usually comes without a reported flood. The model
check finds **no relationship** between how often a barangay floods in the
news and how badly the model rates its nodes (Spearman rho −0.02, 95%
interval −0.42 to +0.36, 23 barangays). With so few barangays that is "no
evidence either way", not proof that the model is wrong.

Reproduce with `python -m scripts.flood_rain_thresholds` and
`python -m scripts.validate_against_flood_history` (both offline; `--json`
for the numbers).

## The data

| Step | Count |
|---|---|
| Groundsource records inside Mandaue | 357 |
| Under 10 km² (barangay-sized) | 299 (research: 299) |
| Dropped: city-sized, about 30 km² | 58 |
| Dropped: centroid in no barangay polygon | 3 |
| Kept | 296 |
| Events after merging repeats within 3 days | 277 |
| Of these, possibly a follow-up story (4–14 days after the barangay's previous event) | 39 |
| Distinct flood days, 2011-07-01 to 2025-11-18 | 84 of 5,478 days with rain data |

The 5 km² cut gives identical results: no record falls between 5 and
10 km² (the largest barangay-sized one is 2.3 km²). Open question 2 of the
plan is therefore moot for this data.

Rain: Open-Meteo's archive of ERA5 reanalysis (about 25 km cells, hourly),
cached in `data/rain/`. An event counts on its first reported day.

**Sanity checks.**
- Records under 10 km²: 299, as in the research.
- 2025-08-15 (the Butuanon flash flood, one death) is a flood day, in 11
  barangays.
- Flood days are rainier on average: 16.8 mm over 24 hours against 7.1 mm
  on other days.

## City-wide thresholds

Reported day (84 flood days against 5,394 others). AUC is the chance a flood
day had more of the feature than a non-flood day. The threshold is the one
that best balances hits and false alarms (Youden's J).

| Feature | Mean, flood days (mm) | Mean, other days (mm) | AUC | Threshold (mm) | Hit rate | False-alarm rate |
|---|---|---|---|---|---|---|
| Wettest 1 h | 3.9 | 2.0 | 0.71 | 1.0 | 88% | 55% |
| Wettest 3 h | 7.7 | 3.6 | 0.73 | 4.2 | 68% | 30% |
| Wettest 6 h | 11.1 | 5.0 | 0.73 | 4.7 | 75% | 35% |
| 24 h total | 16.8 | 7.1 | 0.75 | 3.1 | 92% | 53% |
| 3 days before | 26.9 | 21.5 | 0.61 | 8.4 | 89% | 66% |

With a ±1 day window (a news date can be the day after the flood; each day
takes the wettest of the three around it, and days next to a flood are left
out of the comparison), the AUCs barely move (0.71–0.73; 0.64 for the
3 days before) and the 24-hour threshold rises to 10.2 mm (hits 82%, false
alarms 42%). The date offset is not what limits the separation.

**Of the days with more than X mm in 3 hours, how many had a reported flood?**

| More than | Days | With a reported flood | Share |
|---|---|---|---|
| 10 mm | 393 | 13 | 3% |
| 20 mm | 81 | 6 | 7% |
| 30 mm | 35 | 4 | 11% |
| 40 mm | 13 | 1 | 8% |
| 60 mm | 5 | 1 | 20% |

Most flood days are not heavy-rain days in this series, and most heavy-rain
days have no reported flood. Three reasons, none separable with this data:
reanalysis smooths short local storms (the 2025-08-15 flood, "under an
hour of heavy rain" in the news, shows 4.7 mm in its wettest hour); news
misses floods; and some reported floods are Butuanon overflows driven by
rain upstream, not over the city.

## Per barangay

13 barangays have at least 8 flood days. On the reported day:

| Barangay | Flood days | AUC, 3 h | AUC, 24 h | 24 h threshold (mm) | Hits | False alarms |
|---|---|---|---|---|---|---|
| Tipolo | 32 | 0.71 | 0.73 | 10.3 | 62% | 22% |
| Subangdaku | 29 | 0.76 | 0.77 | 4.3 | 90% | 46% |
| Pakna-an | 27 | 0.76 | 0.79 | 7.3 | 82% | 31% |
| Banilad | 21 | 0.71 | 0.71 | 3.1 | 90% | 54% |
| Umapad | 21 | 0.66 | 0.70 | 3.1 | 90% | 54% |
| Casuntingan | 15 | 0.81 | 0.86 | 8.1 | 93% | 29% |
| Cabancalan | 14 | 0.74 | 0.76 | 7.5 | 79% | 31% |
| Guizo | 13 | 0.69 | 0.72 | 3.1 | 92% | 54% |
| Tabok | 12 | 0.80 | 0.83 | 12.4 | 75% | 17% |
| Tingub | 12 | 0.77 | 0.81 | 8.2 | 83% | 29% |
| Cambaro | 11 | 0.74 | 0.77 | 7.3 | 91% | 31% |
| Ibabao | 9 | 0.71 | 0.74 | 3.2 | 100% | 53% |
| Maguikay | 9 | 0.72 | 0.75 | 8.2 | 78% | 29% |

The other 15 have 1–7 flood days and get "insufficient data". With 9–32
flood days each, a per-barangay threshold moves by several millimetres if
one or two days are added or removed; read them as a rough order, not a
setting.

## The model check

Per barangay: reported flood days (events after merging), against the
baseline run's hazard for that barangay's nodes, as the mean score and as
the share rated Medium or High. Barangays with fewer than 5 model nodes are
left out: Tabok (0 nodes, 12 flood days), Tingub (2 nodes, 12 flood days),
Casili, Cubacub and Tawason. Recle has no population figure, so it drops
out of the density-controlled numbers.

| | Barangays | Spearman rho | 95% interval | Density partialled out | 95% interval |
|---|---|---|---|---|---|
| Mean hazard, all | 23 | −0.02 | −0.42 to +0.36 | −0.08 (22) | −0.57 to +0.36 |
| Share Medium/High, all | 23 | +0.06 | −0.32 to +0.46 | +0.01 (22) | −0.49 to +0.45 |
| Mean hazard, without riverside | 19 | −0.06 | −0.51 to +0.39 | −0.10 (18) | −0.66 to +0.41 |
| Share Medium/High, without riverside | 19 | −0.00 | −0.49 to +0.44 | −0.03 (18) | −0.60 to +0.46 |

Intervals are 2,000 bootstrap draws over barangays, seed fixed.

Riverside barangays (named in the news of the four Butuanon overflow floods
in the research note): Alang-alang (0 flood days, mean hazard 0.073),
Casuntingan (15, 0.123), Pakna-an (27, 0.089), Umapad (21, 0.100); Tabok
and Tingub are left out for lack of nodes. Removing them doesn't change the
picture.

Some contrasts behind the near-zero rho: Tipolo and Subangdaku are reported
most (32 and 29 days) but rate low (mean 0.057 and 0.050); Canduman,
Pagsabungan and Jagobiao rate among the highest (0.14–0.19) with 4–7 days.
Cambaro and Maguikay (11 and 9 days) have almost no rated nodes.

**What it can and can't show.** It is the first check against data that
didn't come from drAIn's users, and it finds no agreement at barangay
level. It can't say why. A barangay-wide mean of a node hazard is a coarse
measure; the model runs one design storm, not the storms that were
reported; news coverage follows population and roads, which a density
control only partly removes; and Groundsource has no cause per record, so
river and drainage floods are mixed outside the riverside list. A hindcast
with observed rain (roadmap D1) and per-node flood locations are what would
settle it.

## Caveats

- **Reported floods, not all floods.** Coverage leans to central, populated
  barangays and to big events.
- **Reanalysis rain.** About 25 km cells, hourly. Short bursts are
  smoothed, so 1 h figures understate real intensities and thresholds come
  out low. PAGASA Mactan hourly data (Q9) would replace it. ERA5's annual
  total here (about 2,600 mm) is above typical gauge figures for Cebu.
- **Dates.** Groundsource dates are article dates, which can trail the
  flood by a day; the ±1 day results cover that.
- **Follow-ups.** 39 events start 4–14 days after the barangay's previous
  one. They are kept as separate floods; some are probably later coverage.
- **Small samples.** 84 flood days city-wide, 9–32 per barangay, 23
  barangays in the model check. No pass/fail claim is made.
- **Not a calibration.** Nothing in the model was changed or fitted.
