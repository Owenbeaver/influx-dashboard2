#!/usr/bin/env python3
"""Influx Lead Engine — Streamlit Dashboard"""

import base64
import csv
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import psutil
import streamlit as st
from dotenv import load_dotenv

# ── Load .env before anything else ────────────────────────────────────────────
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH, override=True)    # .env always takes priority over system env vars

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="INFLUX",
    page_icon="",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap');

*, *::before, *::after { box-sizing: border-box; }

html, body,
[data-testid="stAppViewContainer"],
[data-testid="stMain"],
.main { background: #FAFAF8 !important; font-family: 'Inter', sans-serif !important; }

[data-testid="stHeader"]  { background: transparent !important; }
section[data-testid="stSidebar"] { background: #F0F0EE !important; }
#MainMenu, footer { visibility: hidden; }

/* ── Header ── */
.ifl-header { padding: 28px 0 24px; border-bottom: 1px solid #E5E5E3; margin-bottom: 28px; }
.ifl-wordmark {
    font-family: 'Inter', sans-serif;
    font-weight: 900;
    font-size: 2.6rem;
    letter-spacing: -1px;
    color: #111;
    line-height: 1;
    display: flex;
    align-items: center;
    gap: 16px;
}
.ifl-wordmark img { height: 44px; width: auto; }
.ifl-tag { color: #9CA3AF; font-size: 0.8rem; font-weight: 400; margin-top: 6px; letter-spacing: 0.2px; }

/* ── Metric cards ── */
.mcard {
    background: #111;
    border-radius: 12px;
    padding: 22px 22px 16px;
    height: 100%;
}
.mcard-label {
    color: #6B7280;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    margin-bottom: 10px;
}
.mcard-value {
    font-size: 2.6rem;
    font-weight: 800;
    line-height: 1;
    color: #fff;
    font-variant-numeric: tabular-nums;
    letter-spacing: -1px;
}
.mcard-sub { color: #374151; font-size: 0.73rem; margin-top: 6px; }
.mcard-value.accent { color: #4ADE80; }

/* ── Section label ── */
.sec-label {
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.2px;
    color: #9CA3AF;
    margin: 24px 0 8px 1px;
}

/* ── Log box ── */
.logbox {
    background: #111;
    border-radius: 12px;
    padding: 16px 18px;
    height: 400px;
    overflow-y: scroll;
    font-family: 'Cascadia Code', 'Consolas', 'Courier New', monospace;
    font-size: 0.75rem;
    line-height: 1.75;
    white-space: pre-wrap;
    word-break: break-all;
}
.lg-h  { color: #93C5FD; font-weight: 600; }
.lg-ok { color: #4ADE80; }
.lg-er { color: #F87171; }
.lg-sc { color: #C084FC; }
.lg-ap { color: #FCD34D; }
.lg-rt { color: #FB923C; }
.lg-mt { color: #1F2937; }

/* ── Stopped banner ── */
.stopped-banner {
    background: #FEF2F2;
    border: 1.5px solid #FECACA;
    border-radius: 10px;
    padding: 12px 18px;
    color: #B91C1C;
    font-weight: 700;
    font-size: 0.88rem;
    margin-top: 10px;
}

/* ── Inputs ── */
.stTextInput input {
    background: #fff !important;
    border: 1.5px solid #E5E5E3 !important;
    border-radius: 8px !important;
    color: #111 !important;
    font-size: 0.85rem !important;
    padding: 10px 12px !important;
}
.stTextInput input:focus { border-color: #111 !important; box-shadow: none !important; }
.stTextInput label { color: #6B7280 !important; font-size: 0.78rem !important; font-weight: 600 !important; }

/* ── File uploader ── */
[data-testid="stFileUploader"] section {
    background: #fff !important;
    border: 1.5px dashed #D1D5DB !important;
    border-radius: 10px !important;
}
[data-testid="stFileUploader"] label { color: #6B7280 !important; }

/* ── Expander ── */
[data-testid="stExpander"] {
    background: #fff !important;
    border: 1.5px solid #E5E5E3 !important;
    border-radius: 12px !important;
    box-shadow: none !important;
}
[data-testid="stExpander"] summary p { color: #6B7280 !important; font-weight: 600 !important; font-size: 0.82rem !important; }

/* ── Run button (primary = black) ── */
button[kind="primary"] {
    background: #111 !important;
    border: none !important;
    border-radius: 10px !important;
    color: #fff !important;
    font-weight: 700 !important;
    font-size: 0.92rem !important;
    padding: 14px 0 !important;
    letter-spacing: 0.3px !important;
    transition: background 0.15s !important;
}
button[kind="primary"]:hover    { background: #222 !important; }
button[kind="primary"]:disabled { background: #D1D5DB !important; color: #9CA3AF !important; }

/* ── Stop button — bright red, impossible to miss ── */
.stop-btn button {
    background: #DC2626 !important;
    border: none !important;
    border-radius: 10px !important;
    color: #fff !important;
    font-weight: 800 !important;
    font-size: 0.92rem !important;
    padding: 14px 0 !important;
    letter-spacing: 0.5px !important;
    width: 100% !important;
    cursor: pointer !important;
    transition: background 0.15s !important;
}
.stop-btn button:hover { background: #B91C1C !important; }

/* ── Secondary button ── */
button[kind="secondary"] {
    background: #fff !important;
    border: 1.5px solid #E5E5E3 !important;
    border-radius: 8px !important;
    color: #111 !important;
    font-weight: 600 !important;
    font-size: 0.83rem !important;
}
button[kind="secondary"]:hover { border-color: #111 !important; }

/* ── Download button ── */
[data-testid="stDownloadButton"] button {
    background: #111 !important;
    border: none !important;
    color: #4ADE80 !important;
    font-weight: 700 !important;
    border-radius: 10px !important;
    padding: 14px 0 !important;
    font-size: 0.92rem !important;
    letter-spacing: 0.3px !important;
}
[data-testid="stDownloadButton"] button:hover { background: #222 !important; }

/* ── Divider ── */
hr { border: none; border-top: 1px solid #E5E5E3 !important; margin: 20px 0 !important; }

/* ── Login screen ── */
.login-wrap {
    padding-top: 72px;
    display: flex;
    justify-content: center;
    margin-bottom: 20px;
}
</style>
""", unsafe_allow_html=True)

# ── Session state ──────────────────────────────────────────────────────────────
_DEFAULTS = dict(
    running=False, done=False, stopped=False,
    log_lines=[], results=[],
    log_q=queue.Queue(),
    proc_container={},          # {"process": Popen} written by worker thread
    output_path="", total=0, start_ts=None,
    admin_unlocked=False, admin_error="",
    authenticated=False,
    session_id=None,
)
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Constants ──────────────────────────────────────────────────────────────────
PIPELINE        = Path(__file__).parent / "pipeline.py"
ADMIN_PASS      = os.environ.get("ADMIN_PASSWORD", "influx2024")
DASHBOARD_PASS  = os.environ.get("DASHBOARD_PASSWORD", "")
ACCESS_LOG      = Path(__file__).parent / "access_log.csv"

def _env_key(name: str) -> str:
    return os.environ.get(name, "")


def _log_access(session_id: str, success: bool) -> None:
    """Append one row to access_log.csv."""
    write_header = not ACCESS_LOG.exists()
    try:
        with open(ACCESS_LOG, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(["timestamp", "session_id", "result"])
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                session_id,
                "success" if success else "failed",
            ])
    except Exception:
        pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _logo_html() -> str:
    p = Path(__file__).parent / "influx_logo.png"
    if p.exists():
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f'<img src="data:image/png;base64,{b64}" style="height:42px;width:auto;display:inline-block;">'
    return ""


def _colorize(line: str) -> str:
    l   = line.rstrip()
    esc = l.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    if any(x in l for x in ("====", "Handle:")):
        return f'<span class="lg-h">{esc}</span>'
    if any(x in l for x in ("SELECTED", "Retry] Accepted", "Done!")):
        return f'<span class="lg-ok">{esc}</span>'
    if "email=" in l and "phone=" in l and "[Apollo]" not in l:
        return f'<span class="lg-ok">{esc}</span>'
    if any(x in l for x in ("ERROR", "X No confirmed", "REJECTED", "X Run ended")):
        return f'<span class="lg-er">{esc}</span>'
    if any(x in l for x in ("SCORE", " pts", "S1_", "S2_", "S_name", "S3_", "S5_")):
        return f'<span class="lg-sc">{esc}</span>'
    if "[Retry]" in l:
        return f'<span class="lg-rt">{esc}</span>'
    if any(x in l for x in ("[Apollo]","[Instagram]","[LinkedIn]","[Claude]","[Website]")):
        return f'<span class="lg-ap">{esc}</span>'
    if l.strip() == "" or "    ..." in l:
        return f'<span class="lg-mt">{esc}</span>'
    return esc


def _read_csv(path: str) -> list:
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _confidence(row: dict) -> str:
    if row.get("error"):
        return "Failed"
    if row.get("email") and row.get("phone"):
        return "High"
    if row.get("email") or row.get("phone"):
        return "Medium"
    if row.get("linkedin_url"):
        return "Low"
    return "Failed"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate the process and all its children using psutil."""
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        # Kill children first
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        # Terminate parent gracefully, then force-kill after 2 s
        parent.terminate()
        try:
            parent.wait(timeout=2)
        except psutil.TimeoutExpired:
            parent.kill()
    except psutil.NoSuchProcess:
        pass   # already dead
    finally:
        try:
            proc.kill()   # belt-and-suspenders on the Popen handle
        except Exception:
            pass


def _pipeline_worker(cmd, env, log_q, proc_container: dict) -> None:
    """Run pipeline subprocess; store Popen in proc_container for the stop button."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        proc_container["process"] = proc   # expose to main thread immediately
        for line in proc.stdout:
            log_q.put(line)
        proc.wait()
    except Exception as e:
        log_q.put(f"\n[Dashboard] Process error: {e}\n")
    finally:
        log_q.put(None)  # sentinel — signals done or stopped


# ── Session ID ─────────────────────────────────────────────────────────────────
if st.session_state.session_id is None:
    st.session_state.session_id = str(uuid.uuid4())[:8]

# ── Login gate ─────────────────────────────────────────────────────────────────
if not st.session_state.authenticated:
    _login_logo = _logo_html()
    st.markdown(f"""
    <div class="login-wrap">
      <div class="ifl-wordmark">{_login_logo}INFLUX</div>
    </div>
    """, unsafe_allow_html=True)

    _, _lmid, _ = st.columns([1.5, 1, 1.5])
    with _lmid:
        with st.form("login_form"):
            _pw = st.text_input("", type="password", placeholder="Password",
                                label_visibility="collapsed")
            _submitted = st.form_submit_button(
                "Access Dashboard", type="primary", use_container_width=True
            )
        if _submitted:
            if DASHBOARD_PASS and _pw == DASHBOARD_PASS:
                _log_access(st.session_state.session_id, True)
                st.session_state.authenticated = True
                st.rerun()
            else:
                _log_access(st.session_state.session_id, False)
    st.stop()


# ── Header ─────────────────────────────────────────────────────────────────────
logo = _logo_html()
st.markdown(f"""
<div class="ifl-header">
  <div class="ifl-wordmark">{logo}INFLUX</div>
  <div class="ifl-tag">Instagram → LinkedIn → Email + Phone &nbsp;·&nbsp; Apify · Claude · SerpAPI · Apollo</div>
</div>
""", unsafe_allow_html=True)

# ── Summary bar ────────────────────────────────────────────────────────────────
results   = st.session_state.results
n_total   = len(results)
n_emails  = sum(1 for r in results if r.get("email"))
n_phones  = sum(1 for r in results if r.get("phone"))
n_success = sum(1 for r in results if not r.get("error"))
pct       = lambda a, b: f"{a/b*100:.0f}%" if b else "—"

c1, c2, c3, c4 = st.columns(4)
for col, label, val, sub, accent in [
    (c1, "Processed",    str(n_total),           f"of {st.session_state.total} accounts", False),
    (c2, "Emails Found", str(n_emails),           pct(n_emails,  n_total),                False),
    (c3, "Phones Found", str(n_phones),           pct(n_phones,  n_total),                False),
    (c4, "Success Rate", pct(n_success, n_total), "email or phone found",                 True),
]:
    val_class = "mcard-value accent" if accent and n_total else "mcard-value"
    col.markdown(f"""
    <div class="mcard">
      <div class="mcard-label">{label}</div>
      <div class="{val_class}">{val}</div>
      <div class="mcard-sub">{sub}</div>
    </div>""", unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ── File upload ────────────────────────────────────────────────────────────────
uploaded = st.file_uploader(
    "Upload account CSV — must have a **handle** column with Instagram URLs",
    type=["csv"],
)

# ── Run / Stop buttons ────────────────────────────────────────────────────────
keys_ready = all([
    _env_key("APIFY_TOKEN"), _env_key("SERPAPI_KEY"),
    _env_key("APOLLO_KEY"),  _env_key("ANTHROPIC_API_KEY"),
])

if st.session_state.running:
    # Show bright red Stop button
    st.markdown('<div class="stop-btn">', unsafe_allow_html=True)
    stop_clicked = st.button("⏹  Stop Pipeline", key="stop_btn", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    if stop_clicked:
        proc = st.session_state.proc_container.get("process")
        if proc:
            _kill_process_tree(proc)
        st.session_state.running = False
        st.session_state.done    = False
        st.session_state.stopped = True
        st.rerun()
else:
    can_run = bool(uploaded and keys_ready)
    if st.button("Run Pipeline", disabled=not can_run, type="primary", use_container_width=True):
        tmp      = tempfile.mkdtemp()
        in_path  = os.path.join(tmp, "input.csv")
        out_path = os.path.join(tmp, "output.csv")

        with open(in_path, "wb") as f:
            f.write(uploaded.getvalue())

        with open(in_path, newline="", encoding="utf-8") as f:
            n_handles = sum(1 for r in csv.DictReader(f) if r.get("handle", "").strip())

        env = os.environ.copy()
        env.update({
            "APIFY_TOKEN":         _env_key("APIFY_TOKEN"),
            "SERPAPI_KEY":         _env_key("SERPAPI_KEY"),
            "APOLLO_KEY":          _env_key("APOLLO_KEY"),
            "ANTHROPIC_API_KEY":   _env_key("ANTHROPIC_API_KEY"),
            "WEBHOOK_URL":         _env_key("WEBHOOK_URL"),
            "PIPELINE_INPUT_CSV":  in_path,
            "PIPELINE_OUTPUT_CSV": out_path,
            "PYTHONUNBUFFERED":    "1",
        })

        proc_container: dict = {}
        st.session_state.update(
            running=True, done=False, stopped=False,
            log_lines=[], results=[],
            log_q=queue.Queue(),
            proc_container=proc_container,
            output_path=out_path,
            total=n_handles, start_ts=datetime.now(),
        )

        threading.Thread(
            target=_pipeline_worker,
            args=([sys.executable, "-u", str(PIPELINE), "--full"],
                  env, st.session_state.log_q, proc_container),
            daemon=True,
        ).start()
        st.rerun()

    if not keys_ready:
        st.caption("⚠️  One or more API keys are missing from the .env file. Ask your admin.")

# ── Stopped banner ─────────────────────────────────────────────────────────────
if st.session_state.stopped:
    st.markdown(
        '<div class="stopped-banner">⏹  Pipeline stopped — '
        'all processes killed. Partial results are available below.</div>',
        unsafe_allow_html=True,
    )

# ── Live log ───────────────────────────────────────────────────────────────────
if st.session_state.log_lines or st.session_state.running:
    st.markdown('<div class="sec-label">Live Output</div>', unsafe_allow_html=True)
    log_html = "\n".join(_colorize(ln) for ln in st.session_state.log_lines[-400:])
    st.markdown(f'<div class="logbox">{log_html}</div>', unsafe_allow_html=True)

# ── Results table ──────────────────────────────────────────────────────────────
if st.session_state.results:
    st.markdown('<div class="sec-label">Results</div>', unsafe_allow_html=True)

    df_rows = []
    for r in st.session_state.results:
        conf = _confidence(r)
        slug = r.get("handle","").split("instagram.com/")[-1].rstrip("/") or r.get("handle","")
        df_rows.append({
            "Handle":     f"@{slug}",
            "Name":       r.get("full_name",""),
            "Niche":      (r.get("niche","") or "")[:52],
            "Email":      r.get("email","")  or "—",
            "Phone":      r.get("phone","")  or "—",
            "Confidence": conf,
        })

    df = pd.DataFrame(df_rows)

    _ROW_STYLES = {
        "High":   "background:#F0FDF4; color:#15803D",
        "Medium": "background:#EFF6FF; color:#1D4ED8",
        "Low":    "background:#FFFBEB; color:#B45309",
        "Failed": "background:#FEF2F2; color:#B91C1C",
    }

    def _style_row(row):
        return [_ROW_STYLES.get(row["Confidence"], "")] * len(row)

    styled = df.style.apply(_style_row, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True,
                 height=min(56 + len(df_rows) * 36, 560))

# ── Download button ────────────────────────────────────────────────────────────
if (st.session_state.done or st.session_state.stopped) and os.path.exists(st.session_state.output_path):
    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    with open(st.session_state.output_path, "rb") as f:
        csv_bytes = f.read()
    ts = (st.session_state.start_ts.strftime("%Y%m%d_%H%M")
          if st.session_state.start_ts else "run")
    st.download_button(
        "⬇  Download Results CSV",
        data=csv_bytes,
        file_name=f"influx_leads_{ts}.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.markdown("<br>", unsafe_allow_html=True)
st.markdown("---")

# ── Admin Settings (password-gated) ───────────────────────────────────────────
with st.expander("Admin Settings"):
    if not st.session_state.admin_unlocked:
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        pwd_col, btn_col = st.columns([3, 1])
        with pwd_col:
            entered = st.text_input("Password", type="password",
                                    placeholder="Enter admin password",
                                    label_visibility="collapsed")
        with btn_col:
            if st.button("Unlock", type="secondary"):
                if entered == ADMIN_PASS:
                    st.session_state.admin_unlocked = True
                    st.session_state.admin_error    = ""
                    st.rerun()
                else:
                    st.session_state.admin_error = "Incorrect password."
        if st.session_state.admin_error:
            st.caption(f"❌  {st.session_state.admin_error}")
    else:
        st.success("Admin unlocked")
        st.caption(f"Keys loaded from: `{_ENV_PATH}`")
        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)

        ka, kb = st.columns(2)
        with ka:
            st.text_input("Apify Token",   value=_env_key("APIFY_TOKEN"),        type="password", disabled=True)
            st.text_input("SerpAPI Key",   value=_env_key("SERPAPI_KEY"),        type="password", disabled=True)
        with kb:
            st.text_input("Apollo Key",    value=_env_key("APOLLO_KEY"),         type="password", disabled=True)
            st.text_input("Anthropic Key", value=_env_key("ANTHROPIC_API_KEY"),  type="password", disabled=True)

        st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
        st.caption("To update keys, edit the `.env` file and restart the app.")
        if st.button("Lock", type="secondary"):
            st.session_state.admin_unlocked = False
            st.rerun()

        st.markdown("---")
        st.markdown('<div class="sec-label">Access Log</div>', unsafe_allow_html=True)
        if ACCESS_LOG.exists():
            try:
                _log_df = pd.read_csv(ACCESS_LOG)
                _successes = _log_df[_log_df["result"] == "success"]
                _n_acc   = len(_successes)
                _last_ts = _successes["timestamp"].max() if _n_acc else "Never"
                _la, _lb = st.columns(2)
                _la.metric("Total Logins", _n_acc)
                _lb.metric("Last Access",  _last_ts)
                st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
                st.dataframe(
                    _log_df.iloc[::-1].reset_index(drop=True),
                    use_container_width=True, hide_index=True,
                )
            except Exception as _e:
                st.caption(f"Could not read access log: {_e}")
        else:
            st.caption("No access log yet — first login will create it.")

# ── Auto-refresh while running ─────────────────────────────────────────────────
if st.session_state.running:
    try:
        while True:
            line = st.session_state.log_q.get_nowait()
            if line is None:
                st.session_state.running = False
                st.session_state.done    = True
                break
            st.session_state.log_lines.append(line)
    except queue.Empty:
        pass

    fresh = _read_csv(st.session_state.output_path)
    if fresh:
        st.session_state.results = fresh

    time.sleep(0.6)
    st.rerun()
