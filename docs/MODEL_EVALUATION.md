# Model evaluation

How we prove a new model is better before it touches live alerts.
Every model change (YOLO11m, OSNet, BoT-SORT, SigLIP) is scored on the
same test set with the same command, and the numbers go in the table at
the bottom. **This file holds numbers only — no images, names or alert
ids.**

## The test set

Stored on the server in `./data/eval` (mounted at `/data/eval`), never in
git, root-only (folders `0700`, files `0600`), shown in the app to
**system admins only** (Model Evaluation page). Deleted after
`EVAL_RETENTION_DAYS` (default 365) because it contains people.

| Part | Where it comes from | Labelled by |
|---|---|---|
| Reviewed alerts — Counter Left Unattended, person/intrusion, uniform (`EVAL_TARGET_PER_TYPE`, default 300 each) | Alerts operators marked True/False, with the **clean** frame | The operator's verdict |
| People frames (`EVAL_PEOPLE_TARGET`, default 400) | One frame from a camera that currently sees people, 10:00–20:00 Nairobi, each camera at most every 6 h | Admins click every visible person |
| Clips (`EVAL_CLIP_TARGET`, default 100) | The 30-second alert clip of person / counter alerts | Admins count the different people |

**Why clean frames.** Alert snapshots have boxes and a caption strip drawn
on them, so a detector cannot be scored on them. With collection on, each
new alert of the types above also saves the untouched frame beside its
snapshot (`<snapshot>_raw.jpg`, ~150 KB), deleted with the snapshot after
90 days. Plain "person" alerts have no snapshot, so for those (and for
alerts reviewed before collection was switched on) the frame is cut from
the alert clip while it still exists (48 h).

Collection is **off by default**. Switch it on with
`EVAL_CAPTURE_RAW_FRAMES=true` in `.env`; the collector then runs every
30 minutes and the people sampler hourly. A few hundred reviewed alerts
per type takes as long as operators take to review them — check progress
on the Model Evaluation page.

## Running a comparison

Run in a GPU worker (worker-training is normally idle; prefer quiet hours,
because scoring shares the live GPU):

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.local.yml"

# Today's model (the baseline)
$DC exec worker-training python -m app.eval.run --detector yolov8n.pt --uniform rules

# Two models side by side, plus each value from an earlier report
$DC exec worker-training python -m app.eval.run --detector yolov8n.pt --detector yolo11m.pt \
    --compare /data/eval/reports/report-YYYYMMDD-HHMMSS.json
```

The report prints and is saved as `/data/eval/reports/report-*.json` and
`.md`. It also lists the exact package versions and GPU it ran on.

What the numbers mean:

- **Real alerts kept %** — of the alerts operators marked True, how many
  the model would still support (Counter: nobody in the counter zone;
  person: a person in the zone/frame; uniform: an alerting uniform state).
- **False alerts avoided %** — of the alerts operators marked False, how
  many the model would *not* raise (e.g. it finds the person who was
  actually at the counter).
- **People found % / missed / false detections** — against the admins'
  clicks. A click inside a person box = found; a click with no box =
  missed; a box nobody clicked = false detection. **Edge-of-frame** =
  clicks within 10 % of the picture border (where overhead/fisheye
  cameras struggle).
- **Speed** — milliseconds per frame (median and 95th percentile) through
  the same loader and TensorRT path as live inference.
- **GPU memory** — extra GPU memory the model needs in that process.
- **Alert snapshots/day** — sizes the description-search embedding store.

## GPU headroom at the busiest time

```bash
$DC exec worker-training python -m app.eval.gpu_sample --hours 8 --interval 30
```

Start it in the morning on a busy day; it prints peak / 95th-percentile /
median GPU load, the busiest hour, peak memory and the minimum free
memory, and saves `/data/eval/reports/gpu-*.json`.

## Results

### Environment

| | Value |
|---|---|
| GPU | _to be filled from the first report_ |
| NVIDIA driver | |
| ultralytics / torch / tensorrt | |
| GPU at busiest hour (p95 / peak) | |
| Minimum free GPU memory | |

### Baseline — current models (_date, test-set size_)

| Measure | YOLOv8n (live) |
|---|---|
| Counter Left Unattended: real alerts kept % | |
| Counter Left Unattended: false alerts avoided % | |
| Person/intrusion: real alerts kept % | |
| Person/intrusion: false alerts avoided % | |
| People found % | |
| People missed / false detections | |
| Edge-of-frame people found % | |
| Speed median / p95 (ms per frame) | |
| GPU memory (MB) | |
| Uniform (colour rules): real kept % / false avoided % | |
| Alert snapshots per day (median / max) | |
