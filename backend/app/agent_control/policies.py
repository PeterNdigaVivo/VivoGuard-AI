"""Single accountability registry used by watchdog and scorecards."""


def _p(task: str, interval: int, owner: str, runtime: int = 90, grace: int = 60) -> dict:
    return {"task": task, "interval_seconds": interval, "owner": owner,
            "runtime_sla_seconds": runtime, "start_grace_seconds": grace,
            "availability_target": 0.99, "valid_output_target": 0.99}


AGENT_POLICIES = {
    "ml_dataset": _p("agents.ml_dataset", 6 * 3600, "ML Operations"),
    "training": _p("agents.training", 3600, "ML Operations"),
    "backend_health": _p("agents.backend_health", 1800, "Platform Engineering"),
    "frontend": _p("agents.frontend", 3600, "Platform Engineering"),
    "db_admin": _p("agents.db_admin", 6 * 3600, "Platform Engineering"),
    "streamer": _p("agents.streamer", 300, "CCTV Engineering"),
    "simulation": _p("agents.simulation", 2 * 3600, "ML Quality"),
    "detector_alerts": _p("agents.detector_alerts", 900, "Loss Prevention"),
    "retail_standards": _p("agents.retail_standards", 24 * 3600, "Retail Operations"),
    "inspection": _p("agents.inspection", 24 * 3600, "Loss Prevention"),
    "coverage_assurance": _p("operations.coverage_assurance", 300, "CCTV Engineering"),
    "alert_quality": _p("operations.alert_quality", 300, "Loss Prevention"),
    "lone_worker": _p("operations.lone_worker", 300, "Retail Operations"),
    "event_fusion": _p("operations.event_fusion", 300, "Finance and Loss Prevention"),
    "scenario_simulator": _p("agents.scenario_simulator", 3600, "ML Quality"),
}
