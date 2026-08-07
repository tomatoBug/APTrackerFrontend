import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Import tab20 explicitly from matplotlib.cm
from matplotlib.cm import tab20
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import websockets

# ==============================================================================
# CONFIGURATION - READS STRICTLY FROM ENVIRONMENT VARIABLES
# ==============================================================================

AP_HOST = os.getenv("AP_HOST")
PASSWORD = os.getenv("AP_PASSWORD")

# Parse comma-separated slot names from environment variable
raw_slots = os.getenv("SLOT_NAMES")
SLOT_NAMES = (
    [name.strip() for name in raw_slots.split(",") if name.strip()]
    if raw_slots
    else []
)

# --- Storage Settings ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_DIR = os.path.join(BASE_DIR, "data", "snapshots")
PLOT_DIR = os.path.join(BASE_DIR, "data", "static")

# --- Chart Appearance Settings ---
CHART_TITLE = "Archipelago Multiworld Progress Over Time"
TARGET_TZ = ZoneInfo("Europe/Berlin")  # MESZ / CEST timezone

# Convert tab20 colormap tuples to RGB string format for Plotly
TAB20_COLORS = [
    f"rgb({int(r*255)}, {int(g*255)}, {int(b*255)})"
    for r, g, b, _ in [tab20(i) for i in range(20)]
]

# ==============================================================================
# 1. DATA COLLECTION & SNAPSHOT STORAGE
# ==============================================================================
async def _connect_slot(ws, slot_name):
    """Helper: Authenticates as a specific slot name and retrieves location lists."""
    connect_payload = [
        {
            "cmd": "Connect",
            "password": PASSWORD if PASSWORD else "",
            "name": slot_name,
            "version": {"major": 0, "minor": 6, "build": 0, "class": "Version"},
            "tags": ["Tracker"],
            "items_handling": 0,
            "uuid": str(uuid.uuid4()),
            "game": "",
            "slot_data": False,
        }
    ]
    await ws.send(json.dumps(connect_payload))

    while True:
        raw_msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
        packets = json.loads(raw_msg)
        for pkt in packets:
            cmd = pkt.get("cmd")
            if cmd == "Connected":
                return pkt
            elif cmd == "ConnectionRefused":
                print(
                    f"[WARNING] Refused for slot '{slot_name}': {pkt.get('errors')}"
                )
                return None


async def fetch_and_save_snapshot():
    """Polls all player slots sequentially and saves/overwrites today's daily snapshot in CEST."""
    print(f"[INFO] Connecting to {AP_HOST}...")
    try:
        players_data = []
        mw_total_checked = 0
        mw_total_locations = 0

        for slot_name in SLOT_NAMES:
            async with websockets.connect(AP_HOST, open_timeout=10) as ws:
                slot_pkt = await _connect_slot(ws, slot_name)

                if slot_pkt:
                    slot_id = slot_pkt.get("slot", 0)
                    slot_info = slot_pkt.get("slot_info", {}).get(
                        str(slot_id), {}
                    )
                    client_status = slot_pkt.get("client_status", {})

                    checked = len(slot_pkt.get("checked_locations", []))
                    missing = len(slot_pkt.get("missing_locations", []))
                    total_slot_checks = checked + missing

                    status_code = client_status.get(
                        slot_id, client_status.get(str(slot_id), 0)
                    )

                    players_data.append(
                        {
                            "slot": slot_id,
                            "name": slot_name,
                            "game": slot_info.get("game", "Unknown"),
                            "online": status_code == 10,
                            "completed_goal": status_code == 30,
                            "checks_done": checked,
                            "total_checks": total_slot_checks,
                        }
                    )

                    mw_total_checked += checked
                    mw_total_locations += total_slot_checks
                else:
                    print(
                        f"[WARNING] Could not fetch data for slot '{slot_name}'"
                    )

        if not players_data:
            print("[ERROR] Failed to fetch data for any slots.")
            return False

        now_cest = datetime.now(TARGET_TZ)

        snapshot = {
            "timestamp": now_cest.isoformat(),
            "total_checks": mw_total_locations,
            "completed_checks": mw_total_checked,
            "progress_percent": round(
                (mw_total_checked / mw_total_locations * 100), 2
            )
            if mw_total_locations
            else 0.0,
            "players": sorted(players_data, key=lambda x: x["slot"]),
        }

        os.makedirs(SNAPSHOT_DIR, exist_ok=True)

        today_date_str = now_cest.strftime("%Y-%m-%d")
        filepath = os.path.join(
            SNAPSHOT_DIR, f"snapshot_{today_date_str}.json"
        )

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)

        print(
            f"[INFO] Saved daily snapshot ({today_date_str} CEST) to: {filepath}"
        )
        return True

    except Exception as e:
        print(f"[ERROR] Snapshot creation failed ({AP_HOST}): {e}")
        return False


# ==============================================================================
# 2. MULTI-PANEL PROGRESS PLOTTING (NATIVE DICTIONARY EXTRACTION)
# ==============================================================================
def plot_progression():
    """Reads JSON snapshots, builds Plotly figure, extracts raw dicts, applies Retro Dark styling, and writes apworld1.html."""

    def color_to_rgba(color_str, alpha=0.12):
        """Converts hex or rgb colors to transparent rgba string."""
        if color_str.startswith("rgb("):
            return color_str.replace("rgb(", "rgba(").replace(")", f", {alpha})")
        hex_str = str(color_str).lstrip("#")
        if len(hex_str) == 6:
            r, g, b = tuple(int(hex_str[i : i + 2], 16) for i in (0, 2, 4))
            return f"rgba({r}, {g}, {b}, {alpha})"
        return f"rgba(225, 245, 254, {alpha})"

    if not os.path.exists(SNAPSHOT_DIR):
        print(f"[ERROR] Snapshot directory '{SNAPSHOT_DIR}' does not exist.")
        return

    files = sorted(
        [f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json")]
    )
    if not files:
        print("[ERROR] No JSON snapshot files found in directory.")
        return

    daily_snapshots = {}

    for file_name in files:
        file_path = os.path.join(SNAPSHOT_DIR, file_name)
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            dt_raw = datetime.fromisoformat(data["timestamp"])
            dt_cest = (
                dt_raw.astimezone(TARGET_TZ)
                if dt_raw.tzinfo
                else dt_raw.replace(tzinfo=timezone.utc).astimezone(TARGET_TZ)
            )
            day_key = dt_cest.date()
            daily_snapshots[day_key] = (dt_cest, data)

    timestamps = []
    global_percents = []
    global_completed = []
    global_totals = []
    player_history = {}

    for day_key in sorted(daily_snapshots.keys()):
        dt, data = daily_snapshots[day_key]
        dt_aligned = datetime(dt.year, dt.month, dt.day, tzinfo=TARGET_TZ)
        timestamps.append(dt_aligned)

        tot = data.get("total_checks", 0)
        comp = data.get("completed_checks", 0)
        pct = (comp / tot * 100) if tot > 0 else 0.0

        global_percents.append(pct)
        global_completed.append(comp)
        global_totals.append(tot)

        for p in data.get("players", []):
            s_id = p["slot"]
            p_done = p.get("checks_done", 0)
            p_tot = p.get("total_checks", 0)
            p_pct = (p_done / p_tot * 100) if p_tot > 0 else 0.0

            if s_id not in player_history:
                player_history[s_id] = {
                    "name": p["name"],
                    "game": p["game"],
                    "times": [],
                    "percents": [],
                    "checks_done": [],
                    "total_checks": p_tot,
                    "completed_goal": [],
                }
            player_history[s_id]["times"].append(dt_aligned)
            player_history[s_id]["percents"].append(p_pct)
            player_history[s_id]["checks_done"].append(p_done)
            player_history[s_id]["completed_goal"].append(
                p.get("completed_goal", False)
            )

    # Dynamic date range expansion
    if len(timestamps) > 1:
        date_span = timestamps[-1] - timestamps[0]
        total_seconds = max(date_span.total_seconds(), 86400)
        padding = timedelta(seconds=total_seconds * 0.2)

        x_min = timestamps[0] - timedelta(hours=6)
        x_max = timestamps[-1] + padding
        x_range = [x_min, x_max]
    elif len(timestamps) == 1:
        x_range = [
            timestamps[0] - timedelta(days=1),
            timestamps[0] + timedelta(days=2),
        ]
    else:
        x_range = None

    # Initialize Plotly Subplots
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(CHART_TITLE, "Individual Player Progress"),
    )

    # TOP PANEL: Multiworld Total Progress (%)
    fig.add_trace(
        go.Scatter(
            x=timestamps,
            y=global_percents,
            mode="lines+markers",
            name="Multiworld Progress",
            showlegend=False,
            line=dict(color="#2b5c8f", width=3, shape="hvh"),
            marker=dict(size=7),
            customdata=list(zip(global_completed, global_totals)),
            hovertemplate="<b>Date</b>: %{x|%Y-%m-%d}<br>"
            + "<b>Progress</b>: %{y:.1f}%<br>"
            + "<b>Checks</b>: %{customdata[0]} / %{customdata[1]}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    # TOP ANNOTATION
    if timestamps:
        last_t = timestamps[-1]
        last_pct = global_percents[-1]
        last_comp = global_completed[-1]
        last_tot = global_totals[-1]

        dynamic_ay = -15 - (last_pct * 0.3) if last_pct > 20 else 20

        fig.add_annotation(
            x=last_t,
            y=last_pct,
            xshift=35,
            yshift=0,
            ax=0,
            ay=dynamic_ay,
            text=f"<b>{last_comp} / {last_tot} checks ({last_pct:.1f}%)</b>",
            showarrow=False,
            xanchor="left",
            yanchor="middle",
            font=dict(size=10, color="#2b5c8f"),
            bgcolor="rgba(225, 245, 254, 0.9)",
            bordercolor="#2b5c8f",
            borderwidth=1,
            borderpad=4,
            row=1,
            col=1,
        )

    # BOTTOM PANEL: Per-Player Progress (%)
    sorted_players = sorted(player_history.items())
    annotation_targets = []

    for idx, (slot_num, pdata) in enumerate(sorted_players):
        slot_color = TAB20_COLORS[idx % len(TAB20_COLORS)]
        label_text = f"Slot {slot_num}: {pdata['name']} ({pdata['game']})"

        fig.add_trace(
            go.Scatter(
                x=pdata["times"],
                y=pdata["percents"],
                mode="lines+markers",
                name=label_text,
                showlegend=True,
                line=dict(color=slot_color, width=2, shape="hvh"),
                marker=dict(symbol="square", size=6),
                customdata=list(
                    zip(
                        pdata["checks_done"],
                        [pdata["total_checks"]] * len(pdata["times"]),
                    )
                ),
                hovertemplate=f"<b>{pdata['name']}</b> ({pdata['game']})<br>"
                + "<b>Date</b>: %{x|%Y-%m-%d}<br>"
                + "<b>Progress</b>: %{y:.1f}%<br>"
                + "<b>Checks</b>: %{customdata[0]} / %{customdata[1]}<extra></extra>",
            ),
            row=2,
            col=1,
        )

        completed_times = [
            t
            for t, is_done in zip(pdata["times"], pdata["completed_goal"])
            if is_done
        ]
        completed_pcts = [
            pct
            for pct, is_done in zip(pdata["percents"], pdata["completed_goal"])
            if is_done
        ]

        if completed_times:
            fig.add_trace(
                go.Scatter(
                    x=completed_times,
                    y=completed_pcts,
                    mode="markers",
                    name=f"{pdata['name']} Completed",
                    showlegend=False,
                    marker=dict(
                        symbol="star-diamond",
                        size=14,
                        color="gold",
                        line=dict(color="black", width=1),
                    ),
                    hoverinfo="skip",
                ),
                row=2,
                col=1,
            )

        if pdata["times"]:
            annotation_targets.append(
                {
                    "slot_num": slot_num,
                    "name": pdata["name"],
                    "color": slot_color,
                    "time": pdata["times"][-1],
                    "pct": pdata["percents"][-1],
                    "done": pdata["checks_done"][-1],
                    "total": pdata["total_checks"],
                }
            )

    # EQUIDISTANT ANNOTATION BADGES
    annotation_targets.sort(key=lambda item: item["pct"], reverse=True)

    num_badges = len(annotation_targets)
    if num_badges > 0:
        if num_badges == 1:
            y_positions = [50.0]
        else:
            y_positions = [
                95.0 - (i * (90.0 / (num_badges - 1)))
                for i in range(num_badges)
            ]

        for rank_idx, item in enumerate(annotation_targets):
            bg_tint = color_to_rgba(item["color"], alpha=0.12)

            fig.add_annotation(
                x=item["time"],
                y=y_positions[rank_idx],
                xshift=35,
                yshift=0,
                text=f"<b>S{item['slot_num']} ({item['name']}): {item['done']}/{item['total']} ({item['pct']:.1f}%)</b>",
                showarrow=False,
                xanchor="left",
                yanchor="middle",
                font=dict(size=9, color=item["color"]),
                bgcolor=bg_tint,
                bordercolor=item["color"],
                borderwidth=1,
                borderpad=4,
                row=2,
                col=1,
            )

    # Base Layout configuration
    current_cest_str = datetime.now(TARGET_TZ).strftime(
        "%Y-%m-%d %H:%M:%S CEST"
    )

    fig.update_layout(
        template="plotly_white",
        height=1000,
        legend=dict(
            orientation="v",
            yanchor="top",
            y=0.42,
            xanchor="left",
            x=0.01,
            font=dict(size=9),
            bgcolor="rgba(255, 255, 255, 0.75)",
            bordercolor="#ccc",
            borderwidth=1,
        ),
        margin=dict(l=60, r=40, t=60, b=60),
        title=dict(
            text=f"<i>Last updated: {current_cest_str}</i>",
            font=dict(size=10, color="gray"),
            x=0.99,
            xanchor="right",
            y=0.01,
            yanchor="bottom",
        ),
    )

    for r in (1, 2):
        fig.update_yaxes(
            title_text="Progress (%)" if r == 1 else "Player Progress (%)",
            range=[-2, 105],
            dtick=20,
            showgrid=True,
            gridcolor="#ddd",
            gridwidth=1,
            griddash="dash",
            showline=True,
            linewidth=1,
            linecolor="black",
            mirror=True,
            row=r,
            col=1,
        )

        fig.update_xaxes(
            title_text="Date (CEST)" if r == 2 else None,
            tickformat="%Y-%m-%d",
            range=x_range,
            showgrid=True,
            gridcolor="#eee",
            gridwidth=1,
            griddash="dash",
            showline=True,
            linewidth=1,
            linecolor="black",
            mirror=True,
            row=r,
            col=1,
        )

    # --------------------------------------------------------------------------
    # EXTRACT NATIVE DICTIONARIES & APPLY RETRO DARK PALETTE
    # --------------------------------------------------------------------------
    fig_dict = fig.to_dict()
    data_json = fig_dict.get("data", [])
    layout_json = fig_dict.get("layout", {})
    config_json = {"responsive": True}

    DARK_BG = "#12141c"
    GRID_COLOR = "rgba(64, 224, 208, 0.15)"
    TEXT_COLOR = "#e0fbf8"
    TURQUOISE = "#40e0d0"

    layout_json["paper_bgcolor"] = DARK_BG
    layout_json["plot_bgcolor"] = DARK_BG
    layout_json.setdefault("font", {})["color"] = TEXT_COLOR

    for ann in layout_json.get("annotations", []):
        ann.setdefault("font", {})["color"] = TEXT_COLOR

    for axis_key in ["xaxis", "yaxis", "xaxis2", "yaxis2"]:
        if axis_key in layout_json:
            axis = layout_json[axis_key]
            axis["gridcolor"] = GRID_COLOR
            axis["linecolor"] = TURQUOISE
            axis["zerolinecolor"] = GRID_COLOR
            if "title" in axis and isinstance(axis["title"], dict):
                axis["title"]["font"] = {"color": TEXT_COLOR}

    if "legend" in layout_json:
        layout_json["legend"]["bgcolor"] = "rgba(18, 20, 28, 0.85)"
        layout_json["legend"]["bordercolor"] = "#1f6e66"
        layout_json["legend"]["font"] = {"color": TEXT_COLOR}

    # Assemble target HTML directly matching retro index.html structure
    formatted_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AP Multiworld 1 Progression</title>

    <!-- Prevent caching -->
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate" />
    <meta http-equiv="Pragma" content="no-cache" />
    <meta http-equiv="Expires" content="0" />

    <!-- Google Font for Retro Arcade aesthetic -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">

    <!-- External Retro Arcade Stylesheet -->
    <link rel="stylesheet" href="style.css">

    <!-- Plotly Library -->
    <script charset="utf-8" src="https://cdn.plot.ly/plotly-3.7.0.min.js" integrity="sha256-jvTGqxNp8AGWEcvNLVuKr+8j5dGe9Yw51LQkmDH+IYA=" crossorigin="anonymous"></script>
</head>
<body>

    <header class="page-header">
        <a href="index.html" class="back-button">&larr; BACK TO DASHBOARD</a>
        <h1 class="page-title">AP Multiworld 1 Progression</h1>
    </header>

    <main class="graph-wrapper">
        <div id="plotly-multiworld-chart" class="plotly-graph-div" style="height:100%; width:100%;"></div>
    </main>

    <script>
        window.PlotlyConfig = {{ MathJaxConfig: 'local' }};
        window.PLOTLYENV = window.PLOTLYENV || {{}};

        const chartData = {json.dumps(data_json)};
        const chartLayout = {json.dumps(layout_json)};
        const chartConfig = {json.dumps(config_json)};

        if (document.getElementById("plotly-multiworld-chart")) {{
            Plotly.newPlot("plotly-multiworld-chart", chartData, chartLayout, chartConfig);
        }}
    </script>
</body>
</html>
"""

    os.makedirs(PLOT_DIR, exist_ok=True)
    plot_filepath = os.path.join(PLOT_DIR, "apworld1.html")

    with open(plot_filepath, "w", encoding="utf-8") as f:
        f.write(formatted_html)

    print(
        f"[INFO] Successfully created custom retro HTML plot directly at: {plot_filepath}"
    )


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
if __name__ == "__main__":
    success = asyncio.run(fetch_and_save_snapshot())
    if success:
        plot_progression()
