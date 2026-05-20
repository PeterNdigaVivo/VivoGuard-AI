"""Generate the VivoGuard AI operator + administrator documentation as
a single PDF. Intentionally script-driven (not committed-from-markdown)
so future regenerations stay reproducible and the layout is consistent.

Run from the repo root:
    python scripts/build_docs_pdf.py

Output: docs/VivoGuard-AI-System-Documentation.pdf
"""
from __future__ import annotations
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer,
    Table, TableStyle, PageBreak, KeepTogether,
)


# ---------------------------------------------------------------------------
# Output path

ROOT = Path(__file__).resolve().parent.parent
OUT  = ROOT / "docs" / "VivoGuard-AI-System-Documentation.pdf"
OUT.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Styles

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontSize=20, leading=24,
                    spaceBefore=12, spaceAfter=8, textColor=colors.HexColor("#0c1c3a"))
H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=14, leading=18,
                    spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#1e3a8a"))
H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontSize=11, leading=14,
                    spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#0f172a"))
BODY = ParagraphStyle("Body", parent=ss["BodyText"], fontSize=9.5, leading=13,
                      spaceAfter=4, alignment=TA_LEFT)
SMALL = ParagraphStyle("Small", parent=ss["BodyText"], fontSize=8, leading=10,
                       textColor=colors.HexColor("#475569"))
CODE = ParagraphStyle("Code", parent=ss["BodyText"], fontSize=8.5, leading=11,
                      fontName="Courier", textColor=colors.HexColor("#0f172a"),
                      backColor=colors.HexColor("#f1f5f9"),
                      leftIndent=8, rightIndent=8, spaceBefore=4, spaceAfter=6,
                      borderColor=colors.HexColor("#e2e8f0"), borderWidth=0.5,
                      borderPadding=6)
NOTE = ParagraphStyle("Note", parent=BODY, fontSize=9,
                      textColor=colors.HexColor("#92400e"),
                      backColor=colors.HexColor("#fef3c7"),
                      borderColor=colors.HexColor("#fde68a"), borderWidth=0.5,
                      borderPadding=6, leftIndent=6, rightIndent=6, spaceAfter=6)


# ---------------------------------------------------------------------------
# Element helpers

def p(text: str, style=BODY):
    return Paragraph(text.replace("\n", "<br/>"), style)


def code(lines: list[str]):
    """Render a list of shell/code lines as a monospace block. Escapes
    < and > so reportlab doesn't interpret them as tags."""
    body = "<br/>".join(
        l.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        for l in lines
    )
    return Paragraph(body, CODE)


def kv_table(rows: list[tuple[str, str]], col_widths=(45*mm, 130*mm)):
    """Two-column key/value table used throughout."""
    data = [
        [Paragraph(f"<b>{k}</b>", BODY), Paragraph(v, BODY)]
        for k, v in rows
    ]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def section_table(headers: list[str], rows: list[list[str]], col_widths=None):
    data = [[Paragraph(f"<b>{h}</b>", BODY) for h in headers]]
    for r in rows:
        data.append([Paragraph(c, BODY) for c in r])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a8a")),
        ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f8fafc")]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


# ---------------------------------------------------------------------------
# Page chrome

def _header_footer(canvas, doc):
    canvas.saveState()
    w, h = A4
    # Header bar
    canvas.setFillColor(colors.HexColor("#0c1c3a"))
    canvas.rect(0, h - 14*mm, w, 14*mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(15*mm, h - 9*mm, "VivoGuard AI — System Documentation")
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(w - 15*mm, h - 9*mm, "Vivo Fashion Group · Kenya · Uganda · Rwanda")
    # Footer
    canvas.setFillColor(colors.HexColor("#475569"))
    canvas.setFont("Helvetica", 8)
    canvas.drawString(15*mm, 8*mm,  "Generated by scripts/build_docs_pdf.py")
    canvas.drawCentredString(w / 2, 8*mm,
                              f"Branch: claude/vivoguard-ai-platform-YE5iF")
    canvas.drawRightString(w - 15*mm, 8*mm, f"Page {doc.page}")
    canvas.restoreState()


def make_doc(path: Path) -> BaseDocTemplate:
    doc = BaseDocTemplate(str(path), pagesize=A4,
                          leftMargin=15*mm, rightMargin=15*mm,
                          topMargin=20*mm, bottomMargin=18*mm,
                          title="VivoGuard AI — System Documentation",
                          author="VivoGuard Engineering")
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                  doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame],
                                        onPage=_header_footer)])
    return doc


# ---------------------------------------------------------------------------
# Sections

def section_cover() -> list:
    return [
        Spacer(1, 12*mm),
        Paragraph("VivoGuard AI", ParagraphStyle(
            "Cover1", parent=H1, fontSize=34, leading=38,
            textColor=colors.HexColor("#0c1c3a"))),
        Paragraph("System Documentation &amp; Operator Handbook",
                  ParagraphStyle("Cover2", parent=H1, fontSize=16, leading=20,
                                 textColor=colors.HexColor("#475569"))),
        Spacer(1, 8*mm),
        p("AI-powered video surveillance platform for Vivo Fashion Group's "
          "26 retail stores across Kenya, Uganda, and Rwanda. Built on FastAPI + "
          "Celery + PostgreSQL + Redis + MinIO with YOLOv8 inference."),
        Spacer(1, 6*mm),
        kv_table([
            ("Audience",   "Store managers, regional operators, head-office IT"),
            ("Stack",      "Docker Compose · Python 3.11 · React 18 · Postgres 16 · Redis 7"),
            ("Branch",     "claude/vivoguard-ai-platform-YE5iF"),
            ("Repo path",  "/home/user/VivoGuard-AI"),
            ("First boot", "docker compose up -d --build"),
            ("Login",      "Browser → http://&lt;host&gt;/  (bootstrap admin from env)"),
        ]),
        Spacer(1, 8*mm),
        Paragraph("Contents", H2),
        p(
            "1.  System overview &amp; architecture<br/>"
            "2.  Container topology &amp; ports<br/>"
            "3.  Database schema — key tables<br/>"
            "4.  API surface — endpoints by area<br/>"
            "5.  Detector catalogue<br/>"
            "6.  Camera transport &amp; tunnel modes<br/>"
            "7.  Alerts pipeline &amp; severity<br/>"
            "8.  Scheduled tasks (Celery beat)<br/>"
            "9.  Configuration &amp; environment variables<br/>"
            "10. Deployment commands<br/>"
            "11. Troubleshooting playbook (cameras / API / DB / detectors)<br/>"
            "12. Common one-liners (operator cheat sheet)<br/>"
            "13. Glossary"
        ),
        PageBreak(),
    ]


def section_architecture() -> list:
    return [
        Paragraph("1. System overview &amp; architecture", H1),
        p("VivoGuard AI is a single-tenant deployment that runs entirely on "
          "the operator's hardware. There are no external SaaS dependencies "
          "for the core surveillance pipeline; only Twilio (for SMS / "
          "WhatsApp delivery) and SMTP (for email) are optional integrations."),
        Paragraph("Data flow", H2),
        p(
            "<b>1.  Streamer container</b> opens one connection per camera "
            "(RTSP / TCP, RTSP / HTTP-tunnel, or HTTP snapshot polling) and "
            "publishes each JPEG frame to Redis under <font face='Courier'>"
            "vg:frame:{camera_id}</font>.<br/><br/>"
            "<b>2.  Worker (inference) container</b> consumes those frames, "
            "runs YOLOv8 (CPU by default; GPU when configured), executes the "
            "registered detectors, and writes <b>DetectionEvent</b> + "
            "<b>MetricSnapshot</b> rows to Postgres. Operator-visible "
            "events also create <b>Alert</b> rows.<br/><br/>"
            "<b>3.  API container</b> (FastAPI) exposes the REST surface, "
            "serves the React bundle via nginx (frontend container), and "
            "publishes alert events on the <font face='Courier'>vg:pub:alerts"
            "</font> Redis channel.<br/><br/>"
            "<b>4.  Alert engine</b> (background asyncio task inside API) "
            "subscribes to that channel, deduplicates, and fans out to "
            "SMTP / Twilio SMS / WhatsApp / webhook notifiers in parallel."
            "<br/><br/>"
            "<b>5.  Celery beat</b> drives periodic work: report dispatch, "
            "staff classifier, heatmap archive, morning briefings, scheduled "
            "report generation."
        ),

        Paragraph("Why this shape", H2),
        p("Streamer vs worker is split so the heavy FFmpeg I/O lives next to "
          "the cameras and the ML inference can scale independently. The "
          "streamer image is a thin layer on top of the API image (no torch / "
          "no opencv); the worker image is the API image plus torch + "
          "ultralytics. That keeps the API + streamer footprint under 350 MB."),

        Paragraph("Tiered transport for cameras", H2),
        p("Stores' routers do not consistently forward port 554. The platform "
          "auto-negotiates per camera at first probe:"),
        p(
            "<b>Tier 1 — Direct RTSP</b> over TCP on the configured port "
            "(usually 554, sometimes 7000 on Dahua-Vivo NVRs).<br/>"
            "<b>Tier 2 — RTSP-over-HTTP tunnel</b> using FFmpeg's "
            "<font face='Courier'>-rtsp_transport http</font> flag on an "
            "HTTP admin port. Full video preserved.<br/>"
            "<b>Tier 3 — HTTP snapshot polling</b> using the Dahua "
            "<font face='Courier'>/cgi-bin/snapshot.cgi</font> or Hikvision "
            "<font face='Courier'>/ISAPI/Streaming/channels/N01/picture</font> "
            "or generic ONVIF media URL on an HTTP port. Lower frame rate, "
            "but works wherever a browser does."
        ),
        PageBreak(),
    ]


def section_topology() -> list:
    return [
        Paragraph("2. Container topology &amp; ports", H1),
        section_table(
            ["Service", "Image", "Internal port", "External port", "Purpose"],
            [
                ["api",         "vivoguard/api",      "8000", "via nginx",   "FastAPI REST + WebSocket"],
                ["worker",      "vivoguard/worker",   "—",    "—",           "Celery worker + beat (inference)"],
                ["streamer",    "vivoguard/streamer", "—",    "—",           "FFmpeg / HTTP-snapshot fleet"],
                ["frontend",    "vivoguard/frontend", "80",   "via nginx",   "React bundle (nginx static)"],
                ["postgres",    "postgres:16",        "5432", "—",           "Time-series + entity store"],
                ["redis",       "redis:7",            "6379", "—",           "Pub/sub + frame cache + locks"],
                ["minio",       "minio/minio",        "9000", "—",           "S3-compatible blob store (clips, thumbnails, models)"],
                ["nginx",       "deploy/nginx",       "80",   "80",          "Reverse proxy → api + frontend"],
            ],
            col_widths=(22*mm, 38*mm, 22*mm, 22*mm, 70*mm),
        ),

        Paragraph("Horizontal scaling — streamer shards", H2),
        p("For fleets above ~60 cameras, the streamer can be split across "
          "multiple containers using the bundled overlay:"),
        code([
            "docker compose -f docker-compose.yml \\",
            "               -f docker-compose.scale.yml \\",
            "               up -d --build",
        ]),
        p("Each replica claims <font face='Courier'>cameras WHERE id % "
          "STREAMER_SHARD_COUNT == STREAMER_SHARD_INDEX</font>. Every camera "
          "lands on exactly one replica; no coordination needed."),

        Paragraph("Network", H2),
        p("All containers share the <font face='Courier'>vivoguard</font> "
          "bridge network. The host exposes only nginx on port 80 by default. "
          "Camera traffic stays inside the LAN / VPN — nothing camera-facing "
          "is exposed publicly."),
        PageBreak(),
    ]


def section_schema() -> list:
    return [
        Paragraph("3. Database schema — key tables", H1),
        p("Selected tables most operators interact with via the SQL CLI. "
          "Full schema lives in <font face='Courier'>backend/app/models/"
          "</font> and is materialised by Alembic migrations 0001 → 0011."),

        Paragraph("stores", H2),
        section_table(
            ["Column", "Type", "Notes"],
            [
                ["id",                "int PK",          "—"],
                ["name",              "varchar(128)",    "unique, indexed"],
                ["code",              "varchar(32) NULL", "unique when set"],
                ["country",           "varchar(64)",     "Kenya / Uganda / Rwanda / Other"],
                ["timezone",          "varchar(64)",     "Africa/Nairobi default"],
                ["business_hours_json","jsonb",          "per-weekday open windows"],
                ["capacity",          "int NULL",        "occupancy-alert threshold input"],
                ["manager_name",      "varchar",         "WhatsApp briefing personalisation"],
                ["manager_phone",     "varchar",         "WhatsApp briefing recipient"],
                ["default_rtsp_port", "int NULL",        "inherited by Add Camera wizard"],
            ],
            col_widths=(38*mm, 32*mm, 105*mm),
        ),

        Paragraph("cameras", H2),
        section_table(
            ["Column", "Type", "Notes"],
            [
                ["id, name, brand",   "—",                "brand ∈ dahua | hikvision | uniview | onvif | generic"],
                ["host, public_ip",   "varchar",          "host = local IP or DDNS, public_ip set for WAN"],
                ["rtsp_port, http_port","int",            "rtsp_port often 7000 on Vivo Dahua NVRs"],
                ["channel_number",    "int NULL",         "NVR channel index"],
                ["password_encrypted","text",             "Fernet-encrypted credentials"],
                ["transport",         "varchar(16)",      "rtsp | http_snapshot"],
                ["rtsp_transport",    "varchar(8)",       "tcp | http | udp — FFmpeg flag"],
                ["snapshot_url_override","text NULL",     "set by auto-negotiator on non-Dahua tunnels"],
                ["store_id",          "FK stores.id NULL", "set by Quick Add / store-first onboarding"],
                ["ai_enabled, inference_fps","bool/int",  "—"],
                ["status, last_seen_at, last_error","—",  "informational; Redis health is source of truth"],
            ],
            col_widths=(45*mm, 30*mm, 100*mm),
        ),

        Paragraph("Other key tables", H2),
        kv_table([
            ("metric_snapshots", "Time-series — occupancy, queue_length, queue_wait_seconds, dwell_seconds, staff_present_pct, visitor_count_in, etc. Indexed by (store_id, metric_type, period_start)."),
            ("detection_events", "Raw inference output — one row per detection. detection_type + zone_id + confidence + bbox_json."),
            ("alerts",           "Operator-visible incidents. status ∈ new / confirmed / dismissed / escalated. Joined to detection_events via event_id."),
            ("visitor_tracks",   "Daily-dedup'd person tracks. (store_id, day, track_signature) unique."),
            ("customer_journeys","Per-track zone sequence: zone_sequence_json holds the visit timeline."),
            ("staff_tracks",     "Output of the 10-min staff classifier — counter_seconds + classified_as ∈ staff / customer."),
            ("scheduled_reports","Recurring report definitions — cadence + time_of_day + day_of_week + recipients."),
            ("zones",            "Per-camera polygon / tripwire definitions. detection_types_json drives which detectors run."),
            ("heatmap_snapshots","30-day archive of per-day heatmap PNGs."),
        ]),
        PageBreak(),
    ]


def section_api() -> list:
    return [
        Paragraph("4. API surface — endpoints by area", H1),
        p("All endpoints are mounted under <font face='Courier'>/api</font> "
          "when behind nginx. Auth is JWT bearer on every route except "
          "<font face='Courier'>/auth/login</font> and <font face='Courier'>/"
          "healthz</font>."),

        Paragraph("Auth", H2),
        section_table(
            ["Method", "Path", "Purpose"],
            [
                ["POST", "/auth/login",   "Email + password → access + refresh JWT"],
                ["POST", "/auth/refresh", "Refresh → new access token"],
                ["GET",  "/auth/me",      "Current user"],
            ],
            col_widths=(18*mm, 60*mm, 95*mm),
        ),

        Paragraph("Cameras", H2),
        section_table(
            ["Method", "Path", "Purpose"],
            [
                ["GET",    "/cameras",                              "List"],
                ["POST",   "/cameras/add",                          "Create (single)"],
                ["POST",   "/nvr/quick-add",                        "Bulk-add N channels of one NVR"],
                ["PATCH",  "/cameras/{id}",                         "Update fields"],
                ["DELETE", "/cameras/{id}",                         "Remove"],
                ["GET",    "/cameras/{id}/snapshot",                "Latest JPEG (Redis cache → ffmpeg fallback)"],
                ["POST",   "/cameras/test-rtsp-ports",              "Smart port probe for new cameras"],
                ["POST",   "/cameras/intelligent-probe",            "Multi-port + multi-scheme HTTP snapshot probe"],
                ["POST",   "/cameras/{id}/try-alternate-ports",     "Per-camera RTSP / tunnel port iteration"],
                ["POST",   "/cameras/{id}/transport-diagnose",      "Full probe report (per port + URL)"],
                ["POST",   "/cameras/auto-failover",                "Chain-wide auto switch RTSP → HTTP-snapshot"],
                ["POST",   "/cameras/bulk-update-port",             "Set rtsp_port on every camera at a host"],
                ["GET",    "/cameras/brand-defaults",               "Per-brand default ports for the wizard"],
                ["GET",    "/cameras/cv-quality",                   "Average detection confidence per camera (24h)"],
            ],
            col_widths=(18*mm, 70*mm, 85*mm),
        ),

        Paragraph("Analytics — store dashboard", H2),
        section_table(
            ["Method", "Path", "Purpose"],
            [
                ["GET", "/analytics/store/{id}/live",            "Right Now + Today So Far tiles"],
                ["GET", "/analytics/store/{id}/hourly",          "Hourly footfall + auto-insights"],
                ["GET", "/analytics/store/{id}/week-summary",    "7-day bars + detector activity"],
                ["GET", "/analytics/store/{id}/behaviour",       "Customer journey paths (staff excluded)"],
                ["GET", "/analytics/store/{id}/staff-timeline",  "Per-minute staffed/unstaffed timeline"],
                ["GET", "/analytics/store/{id}/zone-performance","Aisle engagement ranking"],
                ["GET", "/analytics/store/{id}/scorecard",       "Today vs yesterday RAG table"],
                ["GET", "/analytics/store/{id}/anomalies",       "30-day z-score per (weekday, hour)"],
                ["GET", "/analytics/store/{id}/staffing-prediction", "Recommended staff per (weekday, hour)"],
                ["GET", "/analytics/store/{id}/health-score",    "0-100 composite + components"],
                ["GET", "/analytics/store/{id}/loss-prevention", "7-day LP rollup + hotspots"],
                ["GET", "/analytics/store/{id}/behaviour-trends","Weekly trends + conversion funnel"],
                ["GET", "/analytics/store/{id}/visitor-intelligence", "Staff exclusion + dedup"],
                ["GET", "/analytics/dashboard/multi",            "Chain dashboard + RAG per store"],
                ["GET", "/analytics/chain/health-leaderboard",   "Ranked health scores"],
                ["GET", "/analytics/heatmap/{cam}/image",        "PNG (alpha + window=hour|day|week)"],
                ["GET", "/analytics/report.pdf",                 "Ad-hoc PDF (?store_id&amp;since&amp;until)"],
                ["GET", "/analytics/report.csv",                 "Ad-hoc CSV"],
            ],
            col_widths=(18*mm, 72*mm, 85*mm),
        ),

        Paragraph("Alerts &amp; reports", H2),
        section_table(
            ["Method", "Path", "Purpose"],
            [
                ["GET",  "/alerts",                          "List (?store_id, ?status, ?detection_type, …)"],
                ["POST", "/alerts/{id}/resolve",             "Mark resolved (everyday action)"],
                ["POST", "/alerts/{id}/confirm",             "True positive → feeds ML training"],
                ["POST", "/alerts/{id}/dismiss",             "False positive → feeds ML training"],
                ["POST", "/alerts/{id}/note",                "Append investigation note"],
                ["GET",  "/alerts/{id}/snapshot",            "JPEG (event thumb → Redis fallback)"],
                ["GET",  "/reports/scheduled",               "List schedules"],
                ["POST", "/reports/scheduled",               "Create schedule"],
                ["DELETE","/reports/scheduled/{id}",         "Delete"],
                ["POST", "/reports/scheduled/{id}/run-now",  "Queue immediate dispatch"],
            ],
            col_widths=(18*mm, 60*mm, 95*mm),
        ),

        Paragraph("System &amp; diagnostics", H2),
        section_table(
            ["Method", "Path", "Purpose"],
            [
                ["GET", "/healthz",                              "Liveness probe (no auth)"],
                ["GET", "/system/health",                        "Disk / GPU / camera summary"],
                ["GET", "/system/schema-check",                  "Compare ORM expectations vs live DB"],
                ["GET", "/system/cameras/{id}/stream-health",    "Per-camera fps + last frame + errors"],
                ["GET", "/detectors/catalog",                    "All detector types + status badges"],
            ],
            col_widths=(18*mm, 70*mm, 87*mm),
        ),
        PageBreak(),
    ]


def section_detectors() -> list:
    return [
        Paragraph("5. Detector catalogue", H1),
        p("Every detector listed here is implemented and runs on CPU. "
          "Detectors marked <i>training required</i> need a custom YOLO "
          "model — until trained, they're inactive even when enabled."),
        section_table(
            ["Detector", "Plain-English name", "Default threshold", "Alert?"],
            [
                ["occupancy",         "People in Store",       "≥80% of capacity",   "✓"],
                ["queue",             "Checkout Queue",        ">3 people waiting",  "✓"],
                ["dwell",             "Product Interest Time", ">5 min in aisle",    "✓"],
                ["staff_present",     "Counter Staffing",      "Unstaffed >5 min",   "✓"],
                ["intrusion",         "After-hours Security",  "Outside business hrs","✓ critical"],
                ["fight",             "Incident Detection",    "Aggressive motion",   "✓ critical"],
                ["fall",              "Fall Detection",        "Aspect ratio change", "✓ critical"],
                ["weapon",            "Weapon Detected",       "Custom model",       "✓ critical · training required"],
                ["fire / smoke",      "Fire / Smoke",          "Custom model",       "✓ critical · training required"],
                ["shrinkage",         "Loss Prevention",       "Behaviour pattern",  "✓ critical"],
                ["trespass",          "Restricted Area",       "Zone-gated",         "✓"],
                ["loitering",         "Loitering Alert",       ">10 min single area","✓"],
                ["tailgating",        "Tailgate Alert",        "Multi-person entry", "✓"],
                ["abandoned_object",  "Abandoned Item",        "Object > 60 s still","✓"],
                ["crowd",             "Crowd Alert",           ">8 in zone",         "✓"],
                ["shutter",           "Store Open/Close",      "Out-of-hours change","✓"],
                ["stockroom_access",  "Stockroom Access",      "Off-roster entry",   "✓"],
                ["passersby",         "Window Passersby",      "Sidewalk counter",   "—"],
                ["window_engagement", "Window Engagement",     ">5 s window dwell",  "—"],
                ["unique_visitor",    "Customer Count",        "—",                  "— (metric)"],
                ["entry_exit",        "Entry / Exit",          "—",                  "— (metric)"],
                ["heatmap",           "Busiest Areas",         "—",                  "— (metric)"],
                ["customer_journey",  "Customer Journey",      "—",                  "— (metric)"],
                ["demographic",       "Visitor Demographics",  "—",                  "— (privacy)"],
                ["lpr",               "Plate Recognition",     "Vehicle bbox + OCR", "— (metric)"],
                ["face",              "Face Detection",        "Custom model",       "training required"],
                ["uniform_compliance","Uniform Compliance",    "Custom model",       "training required"],
            ],
            col_widths=(32*mm, 42*mm, 42*mm, 42*mm),
        ),

        Paragraph("Default state", H2),
        p("As of migration 0011, opening the AI Settings page for any "
          "camera shows every detector pre-checked with the thresholds above. "
          "Operators just need to click Save once to persist the row; the "
          "inference worker picks it up within ~10 s."),
        PageBreak(),
    ]


def section_transport() -> list:
    return [
        Paragraph("6. Camera transport &amp; tunnel modes", H1),

        Paragraph("Probe order (auto-failover &amp; Test RTSP)", H2),
        section_table(
            ["#", "Step", "Latency budget", "Outcome"],
            [
                ["1", "TCP probe of (host, rtsp_port)",       "2 s",   "If reachable → keep RTSP"],
                ["2", "RTSP tunnel: ffprobe -rtsp_transport http on 7000, 80, 800, 8000, 8080","8 s/port","First port that negotiates wins"],
                ["3", "HTTP snapshot: GET /cgi-bin/snapshot.cgi (Dahua) and equivalents (Hikvision ISAPI, ONVIF, Axis)","4 s/URL","First URL that returns JPEG magic wins"],
                ["4", "Fail",                                 "—",     "Camera stays RTSP, tile shows Diagnose button"],
            ],
            col_widths=(8*mm, 78*mm, 28*mm, 42*mm),
        ),

        Paragraph("Per-brand URL templates", H2),
        section_table(
            ["Brand", "Template"],
            [
                ["Dahua",     "rtsp://USER:PASS@HOST:PORT/cam/realmonitor?channel=N&amp;subtype=0"],
                ["Hikvision", "rtsp://USER:PASS@HOST:PORT/Streaming/Channels/N01"],
                ["Uniview",   "rtsp://USER:PASS@HOST:PORT/unicast/cN/s1/live"],
            ],
            col_widths=(28*mm, 145*mm),
        ),
        p("When tunnel mode wins, the URL is the same — the only difference "
          "is FFmpeg negotiates over the HTTP port instead of opening a "
          "separate 554 connection. Operators don't need to modify any URL."),

        Paragraph("Snapshot probe URL list", H2),
        kv_table([
            ("Dahua-CGI",  "http(s)://host:port/cgi-bin/snapshot.cgi?channel=N"),
            ("Hikvision ISAPI","http(s)://host:port/ISAPI/Streaming/channels/N01/picture"),
            ("Hikvision legacy","http(s)://host:port/Streaming/channels/N01/picture"),
            ("ONVIF snap", "http(s)://host:port/onvif/media_service/snapshot?ProfileToken=Profile_N"),
            ("Axis / Vivotek","http(s)://host:port/axis-cgi/jpg/image.cgi?camera=N"),
        ]),
        p("HTTPS variants attempted automatically with TLS verification off "
          "(self-signed certs on NVRs are normal)."),
        PageBreak(),
    ]


def section_alerts() -> list:
    return [
        Paragraph("7. Alerts pipeline &amp; severity", H1),

        Paragraph("Flow", H2),
        p(
            "<b>1.</b> Detector emits a <font face='Courier'>DetectionEvent"
            "</font>. Most events also create an <font face='Courier'>Alert"
            "</font> row (status=new). Silent metric detectors — heatmap, "
            "customer_journey, unique_visitor, entry_exit, demographic, "
            "occupancy_metrics — never create alerts.<br/><br/>"
            "<b>2.</b> Inference worker publishes the event to Redis "
            "channel <font face='Courier'>vg:pub:alerts</font>.<br/><br/>"
            "<b>3.</b> The alert engine (async task inside the API "
            "container) subscribes, deduplicates per "
            "(camera_id, detection_type, zone_id) within 30 s (5 s for "
            "high-priority), hydrates camera + zone names, and fans out to "
            "every enabled notifier in parallel.<br/><br/>"
            "<b>4.</b> The <font face='Courier'>/alerts</font> page and the "
            "per-store Alerts &amp; Incidents section render "
            "server-rendered title + body + severity + snapshot URL — "
            "the frontend stops translating detection_type strings."
        ),

        Paragraph("Severity buckets", H2),
        section_table(
            ["Bucket", "Types"],
            [
                ["🔴 critical (red)", "fight, intrusion, weapon, weapon_brandished, fall, fire, smoke, shrinkage"],
                ["🟡 warning (amber)", "queue, queue_length, crowd, trespass, loitering, shutter, abandoned_object, tailgating"],
                ["🔵 info (blue)", "staff_present, occupancy, entry_exit, dwell, passersby"],
            ],
            col_widths=(38*mm, 135*mm),
        ),

        Paragraph("Notifiers", H2),
        section_table(
            ["Channel", "Env vars", "Gated on"],
            [
                ["SMTP email", "SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM", "Any event"],
                ["Twilio SMS", "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER", "Any event"],
                ["WhatsApp",   "TWILIO_WHATSAPP_FROM, WHATSAPP_TO, WHATSAPP_PRIORITY_ONLY", "extra.priority == 'high' (default)"],
                ["Webhook",    "WEBHOOK_URL, WEBHOOK_AUTH_HEADER", "Any event"],
            ],
            col_widths=(28*mm, 80*mm, 65*mm),
        ),
        PageBreak(),
    ]


def section_celery() -> list:
    return [
        Paragraph("8. Scheduled tasks (Celery beat)", H1),
        p("All beat ticks run inside the worker container. The worker is "
          "started with the <font face='Courier'>-B</font> flag so beat and "
          "worker share a process — no separate beat container."),
        section_table(
            ["Task", "Schedule", "What it does"],
            [
                ["inference.supervise_all", "30 s",
                 "Spawns a run_camera_inference task per ai_enabled camera"],
                ["maintenance.refresh_ddns", "5 min",
                 "Resolves DDNS hostnames for WAN cameras"],
                ["reports.dispatch_due", "5 min",
                 "Walks scheduled_reports; fires any whose time_of_day + day_of_week match (store-local)"],
                ["heatmap.snapshot_all", "≈ 24 h",
                 "Daily heatmap PNG archive (30-day rolling retention)"],
                ["staff_classifier.classify_today", "10 min",
                 "Flags tracks with >10 min counter-zone dwell as staff"],
                ["briefings.daily_fire_due", "5 min",
                 "Sends 08:00 store-local WhatsApp briefing to manager_phone"],
                ["briefings.weekly_fire_due", "5 min",
                 "Sends Monday 07:00 chain summary to WEEKLY_BRIEFING_TO"],
            ],
            col_widths=(48*mm, 22*mm, 105*mm),
        ),

        Paragraph("Inspecting beat", H2),
        code([
            "# What's registered?",
            "docker compose exec worker celery -A app.tasks.celery_app inspect registered",
            "",
            "# What's running right now?",
            "docker compose exec worker celery -A app.tasks.celery_app inspect active",
            "",
            "# Trigger a task immediately for testing.",
            "docker compose exec worker python -c \"",
            "from app.tasks.briefings import daily_fire_due",
            "daily_fire_due.delay()\"",
        ]),
        PageBreak(),
    ]


def section_env() -> list:
    return [
        Paragraph("9. Configuration &amp; environment variables", H1),
        p("Defined in <font face='Courier'>backend/app/config.py</font>; "
          "operators set them via <font face='Courier'>.env</font> or "
          "<font face='Courier'>docker-compose.override.yml</font>."),

        Paragraph("Required", H2),
        kv_table([
            ("JWT_SECRET",          "32+ char random string"),
            ("BOOTSTRAP_ADMIN_EMAIL","First admin's email — created on first boot"),
            ("BOOTSTRAP_ADMIN_PASSWORD","Initial password (changeable in UI after login)"),
            ("POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB", "Database creds"),
            ("CREDENTIALS_FERNET_KEY","Fernet key for encrypting camera passwords (44-char base64)"),
            ("S3_ACCESS_KEY / S3_SECRET_KEY","MinIO creds (default: minioadmin / minioadmin)"),
        ]),

        Paragraph("Optional — alert delivery", H2),
        kv_table([
            ("SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM", "Email"),
            ("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN", "Twilio creds"),
            ("TWILIO_FROM_NUMBER",       "SMS sender, +<msisdn>"),
            ("TWILIO_WHATSAPP_FROM",     "WhatsApp sender, whatsapp:+<msisdn>"),
            ("WHATSAPP_TO",              "Alert recipients, comma-separated whatsapp:+<msisdn>"),
            ("WHATSAPP_PRIORITY_ONLY",   "true (default) → only high-priority alerts go to WhatsApp"),
            ("WEEKLY_BRIEFING_TO",       "Chain weekly summary recipients (Monday 07:00)"),
            ("WEBHOOK_URL / WEBHOOK_AUTH_HEADER", "Generic webhook destination"),
        ]),

        Paragraph("Optional — operational", H2),
        kv_table([
            ("STREAMER_SHARD_COUNT / STREAMER_SHARD_INDEX", "Set to scale streamers across containers"),
            ("STREAMER_POLL_INTERVAL", "How often the reconcile loop runs (default 5 s)"),
            ("DEFAULT_MODEL",         "YOLOv8 weights path (default yolov8n.pt)"),
            ("INFERENCE_FPS_DEFAULT", "Per-camera FPS when not set on the row"),
            ("USE_GPU / CUDA_VISIBLE_DEVICES", "Toggle GPU inference"),
        ]),
        PageBreak(),
    ]


def section_deploy() -> list:
    return [
        Paragraph("10. Deployment commands", H1),

        Paragraph("First boot (fresh host)", H2),
        code([
            "git clone <repo>",
            "cd VivoGuard-AI",
            "cp .env.example .env       # then edit JWT_SECRET, bootstrap admin, etc.",
            "docker compose up -d --build",
            "# Wait ~60s for postgres + alembic to settle.",
            "docker compose logs --tail=20 api | grep 'alembic upgrade head'",
        ]),

        Paragraph("Standard update", H2),
        code([
            "git pull",
            "docker compose build api worker frontend",
            "docker compose up -d --force-recreate api worker frontend",
        ]),

        Paragraph("Force a clean rebuild (after weird cache behaviour)", H2),
        code([
            "git pull",
            "docker compose build --no-cache api worker frontend streamer",
            "docker compose up -d --force-recreate api worker frontend streamer",
            "# In the browser, hard-refresh: Ctrl+Shift+R",
        ]),

        Paragraph("Scaled streamer (60+ cameras)", H2),
        code([
            "docker compose -f docker-compose.yml \\",
            "               -f docker-compose.scale.yml \\",
            "               up -d --build",
            "# Verify all three replicas:",
            "docker compose logs streamer    | grep 'shard 0 of 3'",
            "docker compose logs streamer-2  | grep 'shard 1 of 3'",
            "docker compose logs streamer-3  | grep 'shard 2 of 3'",
        ]),

        Paragraph("Database backup &amp; restore", H2),
        code([
            "# Backup (run nightly via cron or Task Scheduler)",
            "docker compose exec -T postgres pg_dump -U $POSTGRES_USER \\",
            "    -d $POSTGRES_DB --no-owner --no-acl \\",
            "    | gzip > backups/vivoguard-$(date +%Y%m%d).sql.gz",
            "",
            "# Restore",
            "gunzip -c backups/vivoguard-20260520.sql.gz \\",
            "    | docker compose exec -T postgres psql -U $POSTGRES_USER -d $POSTGRES_DB",
        ]),
        PageBreak(),
    ]


def section_troubleshooting() -> list:
    return [
        Paragraph("11. Troubleshooting playbook", H1),
        p("Symptom-first guide. Run each command in sequence; the first that "
          "returns a useful answer points to the fault."),

        Paragraph("11.1  Site is completely down (browser shows nothing / connection refused)", H2),
        code([
            "docker compose ps",
            "# Look for any service NOT in 'Up' state. Common offenders: api, frontend, postgres.",
            "",
            "docker compose logs --tail=80 api",
            "# Look for: alembic errors, 'Address already in use', bcrypt failures.",
            "",
            "docker compose logs --tail=40 frontend",
            "# nginx errors here mean the static build failed.",
            "",
            "docker compose restart api frontend",
            "# Usually unblocks transient issues.",
            "",
            "# If still down — rebuild without cache:",
            "docker compose build --no-cache api frontend",
            "docker compose up -d --force-recreate api frontend",
        ]),

        Paragraph("11.2  API returns 500 on every request", H2),
        code([
            "docker compose logs --tail=120 api | grep -E 'Traceback|ERROR'",
            "",
            "# Schema drift? (alembic version vs actual columns)",
            "curl -s http://localhost/api/system/schema-check \\",
            "    -H \"Authorization: Bearer $env:VG_TOKEN\" | python -m json.tool",
            "",
            "# If 'missing' is non-empty:",
            "docker compose exec api alembic upgrade head",
        ]),

        Paragraph("11.3  Camera shows 'Stream paused — Connection refused' on Live View", H2),
        code([
            "# 1. Diagnose from the tile: click '🛠 Diagnose & auto-fix'.",
            "#    The modal lists every port and URL that was tried.",
            "",
            "# 2. From CLI, force a TCP probe from inside the api container:",
            "docker compose exec api python -c \"",
            "import asyncio, socket",
            "from app.utils.network import tcp_open",
            "print(asyncio.run(tcp_open('197.155.67.50', 554, timeout=3)))\"",
            "",
            "# 3. Try the alternate-port endpoint directly:",
            "curl -X POST -H \"Authorization: Bearer $env:VG_TOKEN\" \\",
            "    http://localhost/api/cameras/14/try-alternate-ports",
            "",
            "# 4. If RTSP IS unreachable but HTTP works on the NVR's web UI,",
            "#    confirm the camera's rtsp_transport is set to 'http':",
            "docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \\",
            "    -c \"SELECT id,name,host,rtsp_port,rtsp_transport,transport FROM cameras WHERE id=14;\"",
        ]),

        Paragraph("11.4  All cameras dark (streamer not producing frames)", H2),
        code([
            "docker compose ps streamer",
            "docker compose logs --tail=80 streamer",
            "",
            "# Migration drift kills the streamer reconcile loop. Force the",
            "# migration explicitly:",
            "docker compose exec api alembic upgrade head",
            "docker compose restart streamer",
            "",
            "# If the streamer image is stale (rebuilt api but not streamer):",
            "docker compose build --no-cache streamer",
            "docker compose up -d --force-recreate streamer",
            "",
            "# Confirm reconcile is producing specs:",
            "docker compose logs --tail=20 streamer | grep 'reconcile'",
            "# Expect lines like: 'reconcile: 21 desired cameras, 21 running workers'",
        ]),

        Paragraph("11.5  Dashboard shows 0 for queue / staff / dwell", H2),
        code([
            "# Confirm rows exist for the store_id:",
            "docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \\",
            "    -c \"SELECT metric_type, COUNT(*), ROUND(AVG(value)::numeric, 2)",
            "        FROM metric_snapshots",
            "        WHERE store_id = 2",
            "          AND period_start > now() - interval '12 hours'",
            "        GROUP BY metric_type;\"",
            "",
            "# If rows exist but the API reads 0, the camera_id ↔ store_id link is broken.",
            "# As of commit 82b01c4 the analytics filters use",
            "#    (MetricSnapshot.store_id == store_id) OR camera_id IN cam_ids",
            "# so this should self-heal once the api container is on that commit.",
        ]),

        Paragraph("11.6  Detectors are configured but no alerts arrive", H2),
        code([
            "# 1. Confirm the inference worker is processing frames:",
            "docker compose logs --tail=40 worker | grep -i detector",
            "",
            "# 2. Are detection_events being written?",
            "docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \\",
            "    -c \"SELECT detection_type, COUNT(*) FROM detection_events",
            "        WHERE timestamp > now() - interval '1 hour'",
            "        GROUP BY detection_type;\"",
            "",
            "# 3. Are alerts being created?",
            "docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DB \\",
            "    -c \"SELECT status, COUNT(*) FROM alerts",
            "        WHERE created_at > now() - interval '1 hour' GROUP BY status;\"",
            "",
            "# 4. Confirm the alert engine is alive:",
            "docker compose logs --tail=40 api | grep 'alert engine'",
            "# Expect: 'alert engine: subscribed to vg:pub:alerts (notifiers=...)'",
        ]),

        Paragraph("11.7  WhatsApp / email notifications not arriving", H2),
        code([
            "# Twilio: confirm env vars are set on the worker.",
            "docker compose exec worker env | grep -E 'TWILIO|WHATSAPP|SMTP'",
            "",
            "# Fire a test briefing manually:",
            "docker compose exec worker python -c \"",
            "from app.tasks.briefings import daily_fire_due",
            "daily_fire_due.delay()\"",
            "",
            "# Watch logs:",
            "docker compose logs -f worker | grep -iE 'briefing|whatsapp|twilio'",
        ]),

        Paragraph("11.8  Login fails with 401 / 'invalid credentials' immediately after deploy", H2),
        code([
            "# The bcrypt build smoke test catches most cases at build time.",
            "# If it slipped through:",
            "docker compose exec api python -c \"",
            "import bcrypt",
            "h = bcrypt.hashpw(b'test', bcrypt.gensalt(rounds=4))",
            "print(bcrypt.checkpw(b'test', h))\"",
            "# Should print True. If it raises, rebuild api:",
            "docker compose build --no-cache api",
            "docker compose up -d --force-recreate api",
        ]),

        Paragraph("11.9  Reset the admin password", H2),
        code([
            "docker compose exec api python -c \"",
            "from app.database import SessionLocal",
            "from app.models import User",
            "from app.auth.security import hash_password",
            "with SessionLocal() as s:",
            "    u = s.query(User).filter(User.email == 'admin@example.com').first()",
            "    u.password_hash = hash_password('NEW-PASSWORD-HERE')",
            "    s.commit()",
            "    print('reset:', u.email)\"",
        ]),
        PageBreak(),
    ]


def section_oneliners() -> list:
    return [
        Paragraph("12. Common one-liners — operator cheat sheet", H1),

        Paragraph("Health", H2),
        code([
            "# All container statuses",
            "docker compose ps",
            "",
            "# Tail every service",
            "docker compose logs -f --tail=40",
            "",
            "# API liveness (no auth required)",
            "curl http://localhost/api/healthz",
            "",
            "# Detector activity for a store",
            "curl -s -H \"Authorization: Bearer $env:VG_TOKEN\" \\",
            "    http://localhost/api/analytics/store/2/week-summary | python -m json.tool",
        ]),

        Paragraph("Cameras", H2),
        code([
            "# List, with online status",
            "curl -s -H \"Authorization: Bearer $env:VG_TOKEN\" \\",
            "    http://localhost/api/cameras | python -m json.tool",
            "",
            "# Force auto-failover across the fleet",
            "curl -X POST -H \"Authorization: Bearer $env:VG_TOKEN\" \\",
            "    http://localhost/api/cameras/auto-failover",
            "",
            "# Per-camera per-port diagnostic",
            "curl -X POST -H \"Authorization: Bearer $env:VG_TOKEN\" \\",
            "    http://localhost/api/cameras/14/transport-diagnose?fresh=true",
        ]),

        Paragraph("Database", H2),
        code([
            "# Open a psql shell",
            "docker compose exec postgres psql -U $POSTGRES_USER $POSTGRES_DB",
            "",
            "# Recent alerts",
            "SELECT a.id, e.detection_type, c.name, a.status, a.created_at",
            "  FROM alerts a JOIN detection_events e ON a.event_id = e.id",
            "                LEFT JOIN cameras c ON e.camera_id = c.id",
            "  WHERE a.created_at > now() - interval '1 hour'",
            "  ORDER BY a.created_at DESC LIMIT 20;",
            "",
            "# Detector event counts today",
            "SELECT detection_type, COUNT(*)",
            "  FROM detection_events",
            "  WHERE timestamp > date_trunc('day', now())",
            "  GROUP BY 1 ORDER BY 2 DESC;",
        ]),

        Paragraph("Redis", H2),
        code([
            "# Open a redis-cli shell",
            "docker compose exec redis redis-cli",
            "",
            "# Current frame for a camera (size in bytes)",
            "STRLEN vg:frame:14",
            "",
            "# Health record",
            "GET vg:health:14",
            "",
            "# Drop the auto-transport probe cache for one camera",
            "DEL vg:probe:197.155.67.50:554        # internal cache TTL = 5 min anyway",
        ]),

        Paragraph("Tasks", H2),
        code([
            "# Trigger any beat task immediately",
            "docker compose exec worker python -c \"",
            "from app.tasks.staff_classifier import classify_today",
            "classify_today.delay()\"",
            "",
            "# List registered tasks",
            "docker compose exec worker celery -A app.tasks.celery_app inspect registered",
        ]),
        PageBreak(),
    ]


def section_glossary() -> list:
    return [
        Paragraph("13. Glossary", H1),
        kv_table([
            ("Track signature",
             "Per-camera tracker handle in the form 'cam{camera_id}:tr{track_id}'. Same person across two cameras gets two signatures unless ReID is enabled (not yet implemented)."),
            ("Session window",
             "The store's business-hours bounds for today, computed from business_hours_json. Dashboards clamp aggregates to this window so cleaner-shift noise doesn't pollute KPIs."),
            ("Staff classification",
             "A track is flagged 'staff' once it accumulates >10 min of dwell inside a counter-tagged zone today. Analytics LEFT-JOIN visitor_tracks → staff_tracks and exclude staff signatures."),
            ("RAG status",
             "Red / Amber / Green per-store status on the chain dashboard. Red = open AND (no cameras live OR critical alert in last hour). Amber = open AND (staff < 70% OR some cameras offline). Green = otherwise."),
            ("Tunnel mode",
             "FFmpeg's -rtsp_transport http flag. RTSP is negotiated through the HTTP port instead of opening a separate 554 connection. URL syntax is unchanged."),
            ("Detector catalog",
             "Single source-of-truth dict (app/api/detector_catalog.py) mapping every detector_type to its status, default thresholds, and required zone purpose."),
            ("Auto-failover",
             "Per-camera + chain-wide endpoint that probes RTSP/TCP → RTSP/tunnel → HTTP-snapshot and persists whichever combination produces video. Operators rarely need to configure transport manually."),
            ("Heatmap window",
             "vg:heatmap:hour:{id} / day:{id} / week:{id} — three independent per-camera grids with their own reset boundaries. Dashboards request the right key via ?window=hour|day|week."),
            ("Briefing",
             "Daily 08:00 store-local + Monday 07:00 chain WhatsApp summary, delivered via Twilio. Dedup by Redis day-key / iso-week-key."),
            ("Visit quality score",
             "avg_dwell_minutes × (zones_visited / total_zones) × 100. Bounded 0-100ish; surfaced on the Daily performance scorecard's intelligence band."),
        ], col_widths=(40*mm, 135*mm)),
        Spacer(1, 4*mm),

        Paragraph("Document version", H3),
        p("Generated by <font face='Courier'>scripts/build_docs_pdf.py</font>. "
          "Re-run after material changes; commit the regenerated PDF "
          "alongside the source changes so non-technical readers always "
          "have the current document."),
    ]


# ---------------------------------------------------------------------------
# Build

def build() -> Path:
    story: list = []
    story += section_cover()
    story += section_architecture()
    story += section_topology()
    story += section_schema()
    story += section_api()
    story += section_detectors()
    story += section_transport()
    story += section_alerts()
    story += section_celery()
    story += section_env()
    story += section_deploy()
    story += section_troubleshooting()
    story += section_oneliners()
    story += section_glossary()
    doc = make_doc(OUT)
    doc.build(story)
    return OUT


if __name__ == "__main__":
    p = build()
    print(f"wrote {p}  ({p.stat().st_size // 1024} KB)")
