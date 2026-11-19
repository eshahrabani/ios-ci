"""Weighted progress and ETA for one ios-ci desktop session.

The GUI asks GitHub what the run is doing and this module turns that into a percent and
a time-to-ready. No Qt imports: the model is plain data so it can be checked on its own.

The weights come from measured runs on macos-26: the runner queue dominates when the
service is busy, `Prepare the desktop` dominates the job (tailscale plus the streamer
compile on a cache miss), and `Open the desktop` is short once the dead waits are gone.
A step that overruns its weight keeps adding to the ETA at 20% of its own elapsed time,
so the estimate grows instead of sticking at the prior.
"""
import time
from datetime import datetime, timezone

QUEUE_PRIOR = 20.0                      # seconds from dispatch to a started job
STEP_PRIORS = {
    "Set up job": 3.0,
    "Validate the session length": 1.0,
    "Restore the screen streamer": 3.0,
    "Prepare the desktop": 40.0,
    "Open the desktop": 12.0,
}
READY_STEP = "Open the desktop"
DEFAULT_PRIOR = 5.0
# Steps that run after the session was handed over: they are not part of "ready" and
# would otherwise hold the bar below 100% for the whole hold.
AFTER_STEPS = ("Clean up", "Complete job")


def parse_utc(text):
    """Parse the workflow's 2024-01-02T03:04:05Z timestamps."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _seconds(start, end):
    """Seconds between two datetimes; None when either is missing."""
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def short_duration(seconds):
    """A compact duration: 8s, 1m20s, 1h05m."""
    total = int(round(seconds or 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _step_prior(name):
    return STEP_PRIORS.get(name, DEFAULT_PRIOR)


def _job_of(run):
    for job in run.get("jobs") or []:
        if job.get("name") != "plan":
            return job
    return None


def estimate(run, now=None, ready=False):
    """Percent/ETA/phase for one `gh run view --json createdAt,status,conclusion,jobs`."""
    now = time.time() if now is None else now
    created = parse_utc(run.get("createdAt")) if isinstance(run.get("createdAt"), str) \
        else run.get("createdAt")
    elapsed = _seconds(created, datetime.fromtimestamp(now, timezone.utc)) or 0.0
    job = _job_of(run)
    started = None
    if job is not None:
        started = parse_utc(job.get("startedAt")) if isinstance(job.get("startedAt"), str) \
            else job.get("startedAt")
    status = run.get("status") or "queued"

    if job is None or started is None:
        # Nothing has been scheduled yet: only the queue is measurable. 95% is the
        # ceiling, so the bar never claims ready before a step has run.
        phase = "Waiting for a macOS runner"
        fraction = min(0.95, elapsed / QUEUE_PRIOR)
        remaining = max(QUEUE_PRIOR - elapsed, elapsed * 0.2)
        steps = []
        percent = 100.0 * fraction
        eta = None if (status == "completed" and not ready) else remaining
        if ready:
            return {"percent": 100.0, "eta": 0.0, "phase": "Desktop ready",
                    "elapsed": elapsed, "steps": []}
        if status == "completed":
            phase = f"Session {run.get('conclusion') or 'ended'}"
        return {"percent": percent, "eta": eta, "phase": phase, "elapsed": elapsed,
                "steps": steps}

    # The job is running, so the queue is behind us and counts as complete.
    steps = []
    weight_total = QUEUE_PRIOR
    earned = QUEUE_PRIOR
    eta = 0.0
    past_ready = False
    phase = "Starting"
    phase_set = False
    for entry in job.get("steps") or []:
        name = entry.get("name") or ""
        step_status = entry.get("status") or "pending"
        prior = _step_prior(name)
        after = past_ready or name in AFTER_STEPS or name.startswith("Post ")
        if name == READY_STEP:
            past_ready = True
        step_started = parse_utc(entry.get("startedAt")) \
            if isinstance(entry.get("startedAt"), str) else entry.get("startedAt")
        step_done = parse_utc(entry.get("completedAt")) \
            if isinstance(entry.get("completedAt"), str) else entry.get("completedAt")
        step_elapsed = _seconds(step_started, step_done if step_done else (
            datetime.fromtimestamp(now, timezone.utc) if step_status == "in_progress"
            else None))
        step_estimate = 0.0
        if after:
            detail = "after the session"
        elif step_status in ("completed", "skipped"):
            detail = short_duration(step_elapsed or 0.0)
        else:
            if step_status == "in_progress":
                spent = step_elapsed or 0.0
                fraction = min(0.95, spent / prior if prior else 0.95)
                step_estimate = max(prior - spent, spent * 0.2)
                detail = f"{short_duration(spent)} / ~{short_duration(step_estimate)}"
            else:
                fraction = 0.0
                step_estimate = prior
                detail = f"~{short_duration(prior)}"
            if not phase_set:
                phase = name or phase
                phase_set = True
        if not after:
            weight_total += prior
            if step_status in ("completed", "skipped"):
                earned += prior
            elif step_status == "in_progress":
                spent = step_elapsed or 0.0
                earned += prior * min(0.95, spent / prior if prior else 0.95)
            eta += step_estimate
        steps.append({"name": name, "status": step_status,
                      "conclusion": entry.get("conclusion"),
                      "elapsed": step_elapsed, "estimate": step_estimate,
                      "detail": detail})

    if not phase_set and phase == "Starting":
        # Every listed step is done but the job is not: the next one is not published
        # yet, so say what is happening rather than show a stale step name.
        phase = "Wrapping up"

    percent = 100.0 * earned / weight_total if weight_total else 0.0
    if ready:
        percent = 100.0
        eta = 0.0
        phase = "Desktop ready"
        for step in steps:
            if step["name"] == READY_STEP:
                step.update(status="completed", conclusion="success", estimate=0.0,
                            detail="done")
    elif status == "completed":
        eta = None
        phase = f"Session {run.get('conclusion') or 'ended'}"
    return {"percent": min(100.0, percent), "eta": eta, "phase": phase,
            "elapsed": elapsed, "steps": steps}
