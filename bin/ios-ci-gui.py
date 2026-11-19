#!/usr/bin/env python3
"""Desktop app for ios-ci sessions: one macOS VM, streamed into a browser page.

Start a session, watch it come up with a percent and an ETA, open the desktop when the
light turns green, copy a local directory into the VM's home, and stop the run when the
work is done. Everything goes through `gh` and `tailscale`: there is no app checkout, no
git, and nothing is opened inside the VM. The session token is generated here and only
its SHA-256 is dispatched, so a run's metadata is not an access credential.

    bin/ios-ci-gui                       # first run creates a venv with PySide6
    XDG_CONFIG_HOME=... bin/ios-ci-gui   # settings.json lives under that directory
"""
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone

from PySide6.QtCore import QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QBrush, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ios_ci_progress import estimate, parse_utc, short_duration

try:                                  # QtWebEngine ships with PySide6-Addons
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:                   # a minimal wheel without the addons
    QWebEngineView = None

RUNNER_DEFAULT = os.environ.get("IOS_CI_RUNNER", "eshahrabani/ios-ci")
WORKFLOW = "ios-ci.yml"
PORT = 6080
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(CONFIG_HOME, "ios-ci-gui")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
DEFAULT_EXCLUDES = (".git", "node_modules", "build", "DerivedData", "Pods", ".gradle")
UPLOAD_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
UPLOAD_LIMIT = 2 * 1024 ** 3          # the VM holds no more than this comfortably

# The ready light: the only thing in the window that answers "can I use the desktop?".
# Gray means wait, green means frames and input are both proven, amber means the desktop
# is up but the input self-test failed (still openable, just not verified).
GREEN_QSS = "QPushButton{background:#15803d;color:white;font-weight:600;padding:10px 16px;border:0;border-radius:6px;}"
AMBER_QSS = "QPushButton{background:#b45309;color:white;font-weight:600;padding:10px 16px;border:0;border-radius:6px;}"
WAIT_QSS = "QPushButton{background:#3f3f46;color:#d4d4d8;font-weight:600;padding:10px 16px;border:0;border-radius:6px;}"

DEFAULT_SETTINGS = {
    "runner_repo": RUNNER_DEFAULT,
    "minutes": 30,
    "upload_dir": "",
    "exclude_defaults": True,
    "last_run_id": None,
    "sessions": {},
}


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_settings():
    """The stored settings, with the session map sanitized (it holds tokens)."""
    stored = {}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        stored = {}
    settings = dict(DEFAULT_SETTINGS)
    if isinstance(stored, dict):
        for key in DEFAULT_SETTINGS:
            if key in stored:
                settings[key] = stored[key]
    last = settings.get("last_run_id")
    settings["last_run_id"] = _int(last) if str(last).isdigit() else None
    sessions = {}
    for key, value in (settings.get("sessions") or {}).items():
        if not str(key).isdigit() or not isinstance(value, dict):
            continue
        if not isinstance(value.get("token"), str):
            continue
        sessions[str(int(key))] = {
            "token": value["token"],
            "minutes": _int(value.get("minutes")),
            "started_at": str(value.get("started_at") or ""),
            "ready_at": str(value.get("ready_at") or ""),
            "url": str(value.get("url") or ""),
        }
    settings["sessions"] = sessions
    return settings


def save_settings(settings):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CONFIG_DIR,
                                             prefix=".settings-", delete=False)
        try:
            json.dump(settings, handle, indent=2, sort_keys=True)
            handle.write("\n")
        finally:
            handle.close()
        os.chmod(handle.name, 0o600)      # the session tokens live here
        os.replace(handle.name, SETTINGS_PATH)
    except OSError as error:
        print(f"warning: could not save {SETTINGS_PATH}: {error}", file=sys.stderr)


def human_bytes(count):
    size = float(count)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000.0
    return f"{size:.1f} GB"


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BackendError(Exception):
    pass


def require_tool(name):
    path = shutil.which(name)
    if not path:
        raise BackendError(f"{name} is not on PATH")
    return path


class Backend:
    """All gh/tailnet work, with no Qt dependency."""

    def __init__(self, runner_repo=RUNNER_DEFAULT):
        self.runner_repo = (runner_repo or RUNNER_DEFAULT).strip()

    # -- subprocess ---------------------------------------------------------------

    def gh(self, *args, timeout=120, stdin=None):
        try:
            result = subprocess.run(["gh", *args], capture_output=True, text=True,
                                    timeout=timeout, input=stdin)
        except FileNotFoundError:
            raise BackendError("gh is not installed (https://cli.github.com)")
        except subprocess.TimeoutExpired:
            raise BackendError(f"gh {args[0]} timed out")
        if result.returncode:
            message = (result.stderr or result.stdout).strip()
            raise BackendError(message or f"gh {' '.join(args)} failed")
        return result.stdout

    # -- setup checks -------------------------------------------------------------

    def checks(self):
        def add(name, probe):
            try:
                return {"name": name, "ok": True, "detail": probe()}
            except Exception as error:      # a failed check is a row, not a crash
                return {"name": name, "ok": False, "detail": str(error)}

        def auth():
            try:
                output = subprocess.run(["gh", "auth", "status"], capture_output=True,
                                        text=True, timeout=30)
            except FileNotFoundError:
                raise BackendError("gh is not installed")
            if output.returncode:
                raise BackendError("gh is not authenticated (gh auth login)")
            lines = (output.stdout + output.stderr).strip().splitlines()
            return lines[1].strip() if len(lines) > 1 else lines[0].strip()

        def tools():
            return ", ".join(require_tool(tool) for tool in ("gh", "jq", "tailscale"))

        def secrets():
            output = self.gh("secret", "list", "--repo", self.runner_repo, "--json", "name")
            have = {item["name"] for item in json.loads(output or "[]")}
            if "TAILSCALE_AUTH_KEY" not in have:
                raise BackendError("missing: TAILSCALE_AUTH_KEY")
            return "TAILSCALE_AUTH_KEY"

        def tailnet():
            path = require_tool("tailscale")
            result = subprocess.run([path, "status"], capture_output=True, text=True,
                                    timeout=30)
            if result.returncode:
                raise BackendError((result.stderr or result.stdout).strip()[:200]
                                   or "tailscale status failed")
            return "reachable"

        return [
            add("gh authenticated", auth),
            add("gh, jq, tailscale", tools),
            add(f"tailnet auth key on {self.runner_repo}", secrets),
            add("tailnet", tailnet),
        ]

    # -- dispatch and runs --------------------------------------------------------

    def _newest_run(self):
        output = self.gh("run", "list", "--repo", self.runner_repo, "--workflow", WORKFLOW,
                         "--limit", "1", "--json", "databaseId")
        runs = json.loads(output or "[]")
        return int(runs[0]["databaseId"]) if runs else 0

    def dispatch(self, minutes, token_hash):
        """Dispatch one session and return its run id once GitHub lists it."""
        correlation = f"gui-{int(time.time())}-{os.getpid()}"
        try:
            baseline = self._newest_run()
        except BackendError:
            baseline = 0

        print(f"dispatching {minutes} min on {self.runner_repo}", flush=True)
        self.gh("workflow", "run", WORKFLOW, "--repo", self.runner_repo, "--ref", "main",
                "-f", f"correlation_id={correlation}",
                "-f", f"session_minutes={minutes}",
                "-f", f"session_token_hash={token_hash}")

        title = f"ios-ci {correlation}"
        deadline = time.time() + 300
        while time.time() < deadline:
            runs = json.loads(self.gh("run", "list", "--repo", self.runner_repo,
                                      "--workflow", WORKFLOW, "--limit", "100",
                                      "--json", "databaseId,displayTitle,conclusion") or "[]")
            for run in runs:
                if run.get("displayTitle") == title:
                    return int(run["databaseId"])
            # A workflow-file error creates a run with no run-name and no jobs; report
            # it now instead of waiting out the five minutes (same rule as ios-ci.sh).
            stuck = [run for run in runs
                     if int(run.get("databaseId", 0)) > baseline
                     and run.get("conclusion") == "startup_failure"]
            if stuck:
                newest = max(stuck, key=lambda run: int(run["databaseId"]))
                raise BackendError(
                    "the run never started: GitHub rejected the workflow file.\n"
                    f"https://github.com/{self.runner_repo}/actions/runs/{newest['databaseId']}")
            time.sleep(2)
        raise BackendError("the dispatched run did not appear within 5 minutes")

    def runs(self, limit=20):
        output = self.gh("run", "list", "--repo", self.runner_repo, "--workflow", WORKFLOW,
                         "--limit", str(limit),
                         "--json", "databaseId,displayTitle,status,conclusion,"
                                   "createdAt,startedAt,url")
        return json.loads(output or "[]")

    def run_detail(self, run_id):
        """createdAt/status/conclusion/url/jobs for one run, or BackendError."""
        output = self.gh("run", "view", str(run_id), "--repo", self.runner_repo,
                         "--json", "createdAt,status,conclusion,url,jobs", timeout=60)
        detail = json.loads(output or "{}")
        if not detail:
            raise BackendError(f"run {run_id} is not visible yet")
        return detail

    def tailnet_peer(self, hostname):
        """The peer address for a runner node, or None while it has not joined."""
        path = require_tool("tailscale")
        try:
            result = subprocess.run([path, "status", "--json"], capture_output=True,
                                    text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode:
            return None
        try:
            peers = (json.loads(result.stdout or "{}") or {}).get("Peer") or {}
        except ValueError:
            return None
        for peer in peers.values():
            if peer.get("HostName") != hostname:
                continue
            for address in peer.get("TailscaleIPs") or []:
                if ":" not in address:
                    return address
            name = (peer.get("DNSName") or "").rstrip(".")
            if name:
                return name
        return None

    def probe(self, host, token):
        """The parsed /stats payload for a session, or None when it is not serving."""
        query = urllib.parse.urlencode({"t": token})
        try:
            connection = http.client.HTTPConnection(host, PORT, timeout=2)
            try:
                connection.request("GET", f"/stats?{query}")
                response = connection.getresponse()
                body = response.read()
                if response.status != 200:
                    return None
            finally:
                connection.close()
            return json.loads(body.decode("utf-8", "replace"))
        except (OSError, http.client.HTTPException, ValueError):
            return None

    def cancel(self, run_id):
        self.gh("run", "cancel", str(run_id), "--repo", self.runner_repo)

    def set_secret(self, name, value):
        self.gh("secret", "set", name, "--repo", self.runner_repo, stdin=value)

    # -- upload -------------------------------------------------------------------

    def build_archive(self, src_dir, archive, excludes):
        """Write a tar.gz of src_dir; returns (file count, uncompressed bytes)."""
        entries = []
        total = 0
        for base, dirs, names in os.walk(src_dir):
            dirs[:] = sorted(name for name in dirs if name not in excludes)
            for name in sorted(names):
                full = os.path.join(base, name)
                if os.path.islink(full):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                entries.append(full)
                total += size
        if not entries:
            raise BackendError("nothing to copy: the directory has no files")
        if total > UPLOAD_LIMIT:
            raise BackendError(f"the directory is {human_bytes(total)}; the limit is 2 GB")
        with tarfile.open(archive, "w:gz") as tar:
            for full in entries:
                arcname = os.path.relpath(full, src_dir).replace(os.sep, "/")
                tar.add(full, arcname=arcname, recursive=False)
        return len(entries), total

    def upload(self, desktop_url, src_dir, name, excludes, report=None):
        """POST a tar.gz of src_dir to the session; replaces ~/<name>."""
        if not UPLOAD_NAME.fullmatch(name or "") or name in (".", ".."):
            raise BackendError("the name may only use letters, digits, dot, dash, "
                               "underscore (1-64 characters)")
        if not os.path.isdir(src_dir):
            raise BackendError(f"{src_dir} is not a directory")
        parsed = urllib.parse.urlparse(desktop_url)
        token = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]
        if not parsed.hostname or not token:
            raise BackendError("the session URL has no host or token")
        port = parsed.port or PORT
        with tempfile.TemporaryDirectory(prefix="ios-ci-upload-") as work:
            archive = os.path.join(work, "upload.tar.gz")
            count, _size = self.build_archive(src_dir, archive, excludes)
            total = os.path.getsize(archive)
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=600)
            try:
                query = urllib.parse.urlencode({"t": token, "name": name})
                connection.putrequest("POST", f"/upload?{query}")
                connection.putheader("Content-Type", "application/gzip")
                connection.putheader("Content-Length", str(total))
                connection.endheaders()
                sent = 0
                with open(archive, "rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        connection.send(chunk)
                        sent += len(chunk)
                        if report:
                            report(sent, total)
                response = connection.getresponse()
                body = response.read().decode("utf-8", "replace")
                status = response.status
            finally:
                connection.close()
        if status != 200:
            raise BackendError(f"copy failed ({status}): {body.strip()[:300]}")
        try:
            result = json.loads(body)
        except ValueError:
            raise BackendError(f"unexpected reply: {body.strip()[:200]}")
        result["sent"] = total
        result["count"] = count
        return result


class Worker(QThread):
    """One background job; functions receive report(sent, total) for progress."""

    # Not `finished`: that is QThread's own thread-finished signal, which says
    # nothing about a successful result or its value.
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn(self.progress.emit))
        except Exception as error:                       # surfaced to the window
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.backend = Backend(self.settings.get("runner_repo"))
        self.sessions = dict(self.settings.get("sessions") or {})
        self.active_run_id = self.settings.get("last_run_id")
        self.active_token = ""
        self.active_minutes = _int(self.settings.get("minutes"), 30) or 30
        self.active_ip = None
        self.runs = []
        self._light = "idle"                   # idle|starting|ready|amber|ended
        self._ready = False
        self._health = None
        self._model = None                     # last estimate() result
        self._detail = None                    # last `gh run view` result
        self._polling = False
        self._uploading = False
        self._auto_uploaded = False
        self._pending_upload = None
        self._last_phase = None
        self._desktop_view = None
        self._workers = set()
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._persist)

        self.setWindowTitle("ios-ci sessions")
        self.resize(1040, 820)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._build_session_tab()
        self._build_desktop_tab()
        self._build_dashboard_tab()
        self._build_setup_tab()
        self.tabs.currentChanged.connect(self._tab_changed)
        self.statusBar().showMessage("ready")
        self._seed()

        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start()
        QTimer.singleShot(200, self.poll)

    # -- construction -------------------------------------------------------------

    def _build_session_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)

        row = QHBoxLayout()
        row.addWidget(QLabel("Minutes"))
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 300)
        self.minutes.setValue(_int(self.settings.get("minutes"), 30) or 30)
        self.minutes.valueChanged.connect(self._touch)
        row.addWidget(self.minutes)
        row.addStretch(1)
        self.start_button = QPushButton("Start session")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.start_session)
        row.addWidget(self.start_button)
        outer.addLayout(row)

        upload = QGroupBox("Copy a local directory (optional)")
        up_form = QFormLayout(upload)
        row = QHBoxLayout()
        self.upload_edit = QLineEdit(self.settings.get("upload_dir") or "")
        self.upload_edit.setPlaceholderText("local directory to copy into the session")
        self.upload_edit.editingFinished.connect(self._touch)
        row.addWidget(self.upload_edit, 1)
        upload_browse = QPushButton("Browse…")
        upload_browse.clicked.connect(self._browse_upload)
        row.addWidget(upload_browse)
        up_form.addRow("Directory", row)
        row = QHBoxLayout()
        stored_dir = self.settings.get("upload_dir") or ""
        self.upload_name = QLineEdit(os.path.basename(stored_dir.rstrip("/")))
        self.upload_name.setPlaceholderText("target name in the VM's home")
        self.upload_name.textChanged.connect(self._upload_note_changed)
        self.upload_name.textChanged.connect(self._touch)
        row.addWidget(self.upload_name, 1)
        self.upload_button = QPushButton("Copy now")
        self.upload_button.clicked.connect(self.upload)
        row.addWidget(self.upload_button)
        up_form.addRow("Target name", row)
        self.exclude_box = QCheckBox("Skip heavy folders (.git, node_modules, build, "
                                     "DerivedData, Pods, .gradle)")
        self.exclude_box.setChecked(bool(self.settings.get("exclude_defaults")))
        self.exclude_box.toggled.connect(self._touch)
        up_form.addRow("", self.exclude_box)
        self.upload_progress = QProgressBar()
        self.upload_progress.setRange(0, 100)
        self.upload_progress.setValue(0)
        up_form.addRow("Progress", self.upload_progress)
        self.upload_note = QLabel()
        self.upload_note.setWordWrap(True)
        up_form.addRow("", self.upload_note)
        outer.addWidget(upload)
        self._upload_note_changed()

        progress = QGroupBox("Session progress")
        grid = QGridLayout(progress)
        self.open_button = QPushButton("○ No session")
        self.open_button.clicked.connect(self.open_desktop)
        grid.addWidget(self.open_button, 0, 0, 1, 4)
        grid.addWidget(QLabel("Progress"), 1, 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        grid.addWidget(self.progress_bar, 1, 1, 1, 3)
        self.phase_label = QLabel("No session.")
        self.phase_label.setWordWrap(True)
        grid.addWidget(self.phase_label, 2, 0, 1, 4)
        self.time_label = QLabel("—")
        grid.addWidget(self.time_label, 3, 0, 1, 4)
        self.steps_table = QTableWidget(0, 4)
        self.steps_table.setHorizontalHeaderLabels(["Phase", "Status", "Elapsed", "Estimate"])
        self.steps_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.steps_table.verticalHeader().setVisible(False)
        self.steps_table.setFixedHeight(170)
        header = self.steps_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        grid.addWidget(self.steps_table, 4, 0, 1, 4)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("shown once the runner is on the tailnet")
        grid.addWidget(self.url_edit, 5, 0)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy_url)
        grid.addWidget(self.copy_button, 5, 1)
        self.run_button = QPushButton("Run on GitHub")
        self.run_button.clicked.connect(self.open_run)
        grid.addWidget(self.run_button, 5, 2)
        self.stop_button = QPushButton("Stop session")
        self.stop_button.clicked.connect(lambda: self.cancel_run(self.active_run_id))
        grid.addWidget(self.stop_button, 6, 0, 1, 4)
        outer.addWidget(progress)
        outer.addStretch(1)
        self.tabs.addTab(tab, "Session")

    def _build_desktop_tab(self):
        self.desktop_tab = QWidget()
        layout = QVBoxLayout(self.desktop_tab)
        row = QHBoxLayout()
        self.desktop_hint = QLabel(
            "The desktop opens here when the session is ready. "
            + ("" if QWebEngineView else "QtWebEngine is unavailable in this build."))
        self.desktop_hint.setWordWrap(True)
        row.addWidget(self.desktop_hint, 1)
        self.desktop_browser_button = QPushButton("Open in browser")
        self.desktop_browser_button.clicked.connect(
            lambda: self._open_in_browser(self.session_url()))
        row.addWidget(self.desktop_browser_button)
        layout.addLayout(row)
        self.desktop_holder = QVBoxLayout()
        layout.addLayout(self.desktop_holder, 1)
        self.tabs.addTab(self.desktop_tab, "Desktop")

    def _build_dashboard_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Recent sessions (double-click a row to open its desktop)"))
        row.addStretch(1)
        self.dashboard_stop = QPushButton("Stop selected")
        self.dashboard_stop.clicked.connect(self.stop_selected_run)
        row.addWidget(self.dashboard_stop)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.poll)
        row.addWidget(refresh)
        layout.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Status", "Run", "Minutes", "Started", "Remaining", "Desktop", "URL"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(6, QHeaderView.Stretch)
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._dashboard_open)
        layout.addWidget(self.table, 1)
        self.tabs.addTab(tab, "Dashboard")

    def _build_setup_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        runner = QGroupBox("Runner")
        runner_form = QFormLayout(runner)
        self.runner_edit = QLineEdit(self.backend.runner_repo)
        self.runner_edit.editingFinished.connect(self._runner_changed)
        runner_form.addRow("Repository", self.runner_edit)
        layout.addWidget(runner)

        checks = QGroupBox("Checks")
        check_layout = QVBoxLayout(checks)
        self.check_table = QTableWidget(0, 3)
        self.check_table.setHorizontalHeaderLabels(["", "Check", "Detail"])
        self.check_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.check_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self.check_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeToContents)
        self.check_table.verticalHeader().setVisible(False)
        self.check_table.setEditTriggers(QTableWidget.NoEditTriggers)
        check_layout.addWidget(self.check_table)
        row = QHBoxLayout()
        self.recheck_button = QPushButton("Re-check")
        self.recheck_button.clicked.connect(self.run_checks)
        row.addWidget(self.recheck_button)
        row.addStretch(1)
        check_layout.addLayout(row)
        layout.addWidget(checks, 1)

        key = QGroupBox("Tailscale auth key")
        key_form = QFormLayout(key)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("tskey-auth-… (reusable auth key, not an OAuth secret)")
        key_form.addRow("TAILSCALE_AUTH_KEY", self.key_edit)
        self.key_button = QPushButton("Set secret")
        self.key_button.clicked.connect(self.set_tailscale_key)
        key_form.addRow("", self.key_button)
        layout.addWidget(key)
        self.tabs.addTab(tab, "Setup")

    def _seed(self):
        """Restore the last session: its token, its minutes, and its URL."""
        entry = self.sessions.get(str(self.active_run_id)) if self.active_run_id else None
        if entry:
            self.active_token = entry.get("token") or ""
            self.active_minutes = _int(entry.get("minutes"), self.active_minutes)
            if entry.get("url"):
                self.url_edit.setText(entry["url"])
            self.phase_label.setText(f"Reconnecting to run {self.active_run_id}…")
            self.report(f"reconnecting to run {self.active_run_id}")
        self._set_light("idle")
        self._update_buttons()

    # -- settings -----------------------------------------------------------------

    def _touch(self):
        self._save_timer.start()

    def _settings_from_widgets(self):
        return {
            "runner_repo": self.backend.runner_repo,
            "minutes": self.minutes.value(),
            "upload_dir": self.upload_edit.text().strip(),
            "exclude_defaults": self.exclude_box.isChecked(),
            "last_run_id": self.active_run_id,
            "sessions": self.sessions,
        }

    def _persist(self):
        self.settings.update(self._settings_from_widgets())
        # Only the newest sessions are reconnected-by-id; older ones would grow the
        # settings file without ever being reachable from the dashboard.
        numeric = sorted((key for key in self.sessions if str(key).isdigit()), key=int)
        self.sessions = {key: self.sessions[key] for key in numeric[-20:]}
        self.settings["sessions"] = self.sessions
        save_settings(self.settings)

    def closeEvent(self, event):
        self.poll_timer.stop()
        self._persist()
        for worker in list(self._workers):
            worker.wait(2000)
        super().closeEvent(event)

    # -- small widgets ------------------------------------------------------------

    def report(self, text, error=False):
        self.statusBar().showMessage(("error: " if error else "") + text, 15000)

    def _set_light(self, state, percent=None):
        """The desktop button: gray while waiting, green when proven, amber on failure."""
        self._light = state
        if state == "ready":
            self.open_button.setText("● Desktop ready — open")
            self.open_button.setStyleSheet(GREEN_QSS)
            self.open_button.setEnabled(True)
        elif state == "amber":
            self.open_button.setText("▲ Desktop up — input check failed — open")
            self.open_button.setStyleSheet(AMBER_QSS)
            self.open_button.setEnabled(True)
        elif state == "ended":
            self.open_button.setText("○ Session ended")
            self.open_button.setStyleSheet(WAIT_QSS)
            self.open_button.setEnabled(False)
        elif state == "starting":
            amount = 0 if percent is None else int(round(percent))
            self.open_button.setText(f"○ Starting desktop — {amount}%")
            self.open_button.setStyleSheet(WAIT_QSS)
            self.open_button.setEnabled(False)
        else:
            self.open_button.setText("○ No session")
            self.open_button.setStyleSheet(WAIT_QSS)
            self.open_button.setEnabled(False)

    def _update_buttons(self):
        self.open_button.setEnabled(self._light in ("ready", "amber"))
        self.copy_button.setEnabled(bool(self.url_edit.text().strip()))
        self.upload_button.setEnabled(bool(self.session_url()) and not self._uploading)
        self.stop_button.setEnabled(bool(self.active_run_id))
        self.run_button.setEnabled(bool(self._run_url()))

    def _upload_note_changed(self):
        name = self.upload_name.text().strip() or "…"
        self.upload_note.setText(
            f"Copied to ~/{name} in the VM after the desktop is ready.")

    def _tab_changed(self, index):
        if index == 3 and self.check_table.rowCount() == 0:
            self.run_checks()

    def _browse_upload(self):
        path = QFileDialog.getExistingDirectory(self, "Directory to copy",
                                                self.upload_edit.text()
                                                or os.path.expanduser("~"))
        if not path:
            return
        previous = os.path.basename(self.upload_edit.text().rstrip("/"))
        self.upload_edit.setText(path)
        if not self.upload_name.text().strip() or self.upload_name.text() == previous:
            self.upload_name.setText(os.path.basename(path))
        self._persist()

    # -- background jobs ----------------------------------------------------------

    def task(self, fn, done, button=None, title=None, progress=None, failed=None):
        worker = Worker(fn, self)
        self._workers.add(worker)
        if button is not None:
            button.setEnabled(False)
        if progress is not None:
            worker.progress.connect(progress)

        def finish(value):
            self._workers.discard(worker)
            if button is not None:
                button.setEnabled(True)
            done(value)

        def fail(message):
            self._workers.discard(worker)
            if button is not None:
                button.setEnabled(True)
            if failed is not None:
                failed(message)
            self._update_buttons()
            self.report(message, error=True)
            if title:
                QMessageBox.warning(self, title, message)

        worker.result.connect(finish)
        worker.failed.connect(fail)
        worker.start()
        return worker

    # -- session lifecycle --------------------------------------------------------

    def _reset_progress(self):
        self.progress_bar.setValue(0)
        self.phase_label.setText("Dispatching the session")
        self.time_label.setText("—")
        self.steps_table.setRowCount(0)
        self.url_edit.clear()
        self._health = None
        self._model = None
        self._detail = None
        self._last_phase = None

    def _upload_snapshot(self):
        """(src, name, excludes) for the automatic copy, or None when there is none."""
        src = self.upload_edit.text().strip()
        if not src:
            return None
        if not os.path.isdir(src):
            self.report(f"not a directory, skipping the automatic copy: {src}", error=True)
            return None
        name = self.upload_name.text().strip()
        if not UPLOAD_NAME.fullmatch(name or "") or name in (".", ".."):
            self.report("the copy name is not usable; skipping the automatic copy",
                        error=True)
            return None
        excludes = DEFAULT_EXCLUDES if self.exclude_box.isChecked() else ()
        return (src, name, excludes)

    def start_session(self):
        minutes = int(self.minutes.value())
        snapshot = self._upload_snapshot()
        token = secrets.token_urlsafe(24)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self._reset_progress()
        self._set_light("starting", 0)
        self.report(f"dispatching a {minutes}-minute session")

        def work(_report):
            return self.backend.dispatch(minutes, token_hash)

        def done(run_id):
            self.active_run_id = int(run_id)
            self.active_token = token
            self.active_minutes = minutes
            self.active_ip = None
            self._ready = False
            self._uploading = False
            self._auto_uploaded = False
            self._pending_upload = snapshot
            self.sessions[str(run_id)] = {"token": token, "minutes": minutes,
                                          "started_at": utc_now(), "ready_at": "",
                                          "url": ""}
            self.report(f"dispatched run {run_id}")
            self._persist()
            self._update_buttons()
            self.poll()

        def failed(_message):
            self._set_light("idle")
            self.phase_label.setText("No session.")
            self._update_buttons()

        self.task(work, done, button=self.start_button, title="Start session",
                  failed=failed)

    def cancel_run(self, run_id):
        if not run_id:
            return
        self.report(f"stopping run {run_id}…")

        def done(_value):
            self.report(f"asked GitHub to cancel run {run_id}")
            self.poll()

        self.task(lambda _report: self.backend.cancel(run_id), done)

    # -- polling ------------------------------------------------------------------

    def poll(self):
        if self._polling:
            return
        self._polling = True
        active = self.active_run_id
        ip_hint = self.active_ip
        token = self.active_token

        def work(_report):
            result = {"runs": None, "detail": None, "ip": None, "health": None}
            try:
                result["runs"] = self.backend.runs()
            except BackendError:
                result["runs"] = None            # keep the previous list on error
            if not active:
                return result
            try:
                result["detail"] = self.backend.run_detail(active)
            except BackendError:
                result["detail"] = None
            if ip_hint:
                result["ip"] = ip_hint
            else:
                result["ip"] = self.backend.tailnet_peer(f"ios-ci-{active}")
            if result["ip"] and token:
                result["health"] = self.backend.probe(result["ip"], token)
            return result

        self.task(work, self._apply_poll, failed=self._poll_failed)

    def _poll_failed(self, message):
        self._polling = False
        self.report(message, error=True)

    def _summary_of_active(self):
        return next((run for run in self.runs
                     if str(run.get("databaseId")) == str(self.active_run_id)), None)

    def _run_url(self):
        if self._detail and self._detail.get("url"):
            return self._detail["url"]
        return (self._summary_of_active() or {}).get("url") or ""

    def _progress_model(self, ready=False):
        """estimate() over the newest run detail, falling back to the run summary.

        A summary has no steps, so it only feeds the model while the run has not been
        scheduled yet; once it is running, the previous model is kept rather than
        regressing to "waiting for a runner".
        """
        source = self._detail
        if not source:
            summary = self._summary_of_active()
            if not summary:
                return self._model
            if (summary.get("status") or "") not in ("queued", "waiting", "pending",
                                                      "requested"):
                return self._model
            source = {"createdAt": summary.get("createdAt"),
                      "status": summary.get("status"),
                      "conclusion": summary.get("conclusion"), "jobs": []}
        return estimate(source, ready=ready)

    def _apply_poll(self, result):
        self._polling = False
        if result.get("runs") is not None:
            self.runs = result["runs"]
            self._refresh_dashboard()
        if result.get("detail"):
            self._detail = result["detail"]
        if result.get("health") is not None:
            self._health = result["health"]
        if not self.active_run_id:
            # The ended state stays until another session starts, so the reason the
            # light went out is still on screen.
            if self._light != "ended":
                self._set_light("idle")
            self._update_buttons()
            return
        ip = result.get("ip")
        if ip:
            self.active_ip = ip
            url = self.session_url()
            if url and url != self.url_edit.text().strip():
                self.url_edit.setText(url)

        detail = self._detail or {}
        summary = self._summary_of_active() or {}
        status = detail.get("status") or summary.get("status")
        conclusion = detail.get("conclusion") or summary.get("conclusion")
        if status == "completed":
            self._finish_session(conclusion)
            return

        health = self._health
        ready = bool(health and health.get("alive") and _int(health.get("frames")) > 0
                     and _int(health.get("width")) > 0
                     and _int(health.get("height")) > 0
                     and health.get("input_ok"))
        if ready:
            self._mark_ready()
            return

        model = self._progress_model()
        if model:
            self._model = model
        percent = (model or {}).get("percent") or 0.0
        if health and health.get("alive") and not health.get("input_checked"):
            self._set_light("starting", percent)
            self._set_progress(model, phase="Verifying the desktop (frames are live)")
        elif health and health.get("input_checked") and not health.get("input_ok"):
            # The desktop is usable, but the probe did not see a click land: open it,
            # do not claim it is verified.
            self._set_light("amber", percent)
            self._set_progress(model, phase="Desktop up — input check failed")
        else:
            self._set_light("starting", percent)
            self._set_progress(model)
        self._session_time()
        self._update_buttons()

    def _mark_ready(self):
        self._ready = True
        self._model = self._progress_model(ready=True) or {
            "percent": 100.0, "eta": 0.0, "phase": "Desktop ready", "elapsed": 0,
            "steps": []}
        self._set_light("ready")
        self._record_ready()
        self._set_progress(self._model)
        self._session_time()
        # The poll refreshed the dashboard before the light was green, so flip the row
        # now instead of leaving it at "starting NN%" until the next tick.
        self._refresh_dashboard()
        self._update_buttons()

    def _record_ready(self):
        """First sighting of a usable desktop: remember when, and copy the directory."""
        entry = self.sessions.get(str(self.active_run_id))
        if entry is not None:
            changed = False
            if not entry.get("ready_at"):
                entry["ready_at"] = utc_now()
                changed = True
            url = self.session_url()
            if url and entry.get("url") != url:
                entry["url"] = url
                changed = True
            if changed:
                self._persist()
        if self._pending_upload and not self._auto_uploaded:
            self._auto_uploaded = True
            src, name, excludes = self._pending_upload
            self._upload_directory(src, name, excludes, auto=True)

    def _finish_session(self, conclusion):
        url = self.session_url()
        self._ready = False
        self._set_light("ended")
        self.phase_label.setText(f"Session ended — {conclusion or 'completed'}")
        self._last_phase = None
        self.desktop_hint.setText("The session has ended.")
        if url:
            self.url_edit.setText(url)
        self.time_label.setText("—")
        self.active_run_id = None
        self.active_token = ""
        self.active_ip = None
        self._detail = None
        self._health = None
        self._pending_upload = None
        self.report(f"session ended — {conclusion or 'completed'}")
        self._persist()
        self._refresh_dashboard()
        self._update_buttons()

    def _session_time(self):
        entry = self.sessions.get(str(self.active_run_id)) or {}
        ready = parse_utc(entry.get("ready_at") or "")
        minutes = _int(entry.get("minutes"), self.active_minutes)
        if not ready or not minutes:
            self.time_label.setText("—")
            return
        end = ready.timestamp() + minutes * 60
        now = time.time()
        left = end - now
        elapsed = int((now - ready.timestamp()) // 60)
        until = datetime.fromtimestamp(end).strftime("%H:%M")
        if left <= 0:
            self.time_label.setText(f"~{until} (session over), {elapsed} min elapsed")
        else:
            self.time_label.setText(
                f"~{until} ({int(left // 60)} min left), {elapsed} min elapsed")

    def _set_progress(self, model, phase=None):
        """Paint one estimate(): bar, phase line, step table, and the desktop hint."""
        if not model:
            return
        percent = model.get("percent") or 0.0
        eta = model.get("eta")
        if phase is None:
            phase = model.get("phase") or ""
        elapsed = int(model.get("elapsed") or 0)
        self.progress_bar.setValue(int(round(percent)))
        parts = [f"{phase} — {percent:.0f}%", f"elapsed {self._clock(elapsed)}"]
        if eta is not None:
            parts.append(f"ETA {self._clock(int(eta))}")
        self.phase_label.setText(" · ".join(parts))
        self._fill_steps(model.get("steps") or [])
        if self._ready:
            self.desktop_hint.setText("Desktop ready — the page below is live.")
        elif eta is None:
            self.desktop_hint.setText(f"Starting the desktop — {percent:.0f}%")
        else:
            self.desktop_hint.setText(
                f"Starting the desktop — {percent:.0f}% (ETA {short_duration(eta)})")
        if phase != self._last_phase:
            self._last_phase = phase
            self.statusBar().showMessage(f"{phase} — {percent:.0f}%", 15000)

    def _fill_steps(self, steps):
        self.steps_table.setRowCount(len(steps))
        for row, step in enumerate(steps):
            after = step.get("detail") == "after the session"
            if after:
                elapsed_text, estimate_text = "—", "after the session"
            else:
                elapsed_text = ("—" if step.get("elapsed") is None
                                else short_duration(step["elapsed"]))
                if step.get("status") in ("completed", "skipped"):
                    estimate_text = ""
                else:
                    estimate_text = f"~{short_duration(step.get('estimate') or 0)}"
            values = [step.get("name") or "", step.get("status") or "",
                      elapsed_text, estimate_text]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if step.get("status") == "in_progress" and not after:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.steps_table.setItem(row, column, item)

    @staticmethod
    def _clock(seconds):
        total = max(0, int(seconds))
        return f"{total // 60:02d}:{total % 60:02d}" if total < 3600 else \
            f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"

    # -- URLs, upload, desktop ----------------------------------------------------

    def session_url(self):
        if self.active_ip and self.active_token:
            return (f"http://{self.active_ip}:{PORT}/?t="
                    + urllib.parse.quote(self.active_token))
        entry = self.sessions.get(str(self.active_run_id)) or {}
        return entry.get("url") or ""

    def upload(self):
        src = self.upload_edit.text().strip()
        name = self.upload_name.text().strip()
        excludes = DEFAULT_EXCLUDES if self.exclude_box.isChecked() else ()
        self._upload_directory(src, name, excludes, auto=False)

    def _upload_directory(self, src, name, excludes, auto=False):
        """Copy one directory into the VM's home; `auto` is the post-ready copy."""
        url = self.session_url()
        if not url:
            message = "no session URL yet"
            self.upload_note.setText("Copy skipped: no session URL.")
            self.report(message, error=True)
            if not auto:
                QMessageBox.warning(self, "Copy", "The desktop is not reachable yet.")
            return
        if not src or not os.path.isdir(src):
            message = f"{src or 'no directory'} is not a directory"
            self.upload_note.setText("Copy skipped: choose a directory.")
            self.report(message, error=True)
            if not auto:
                QMessageBox.warning(self, "Copy", message)
            return
        self._uploading = True
        self.upload_progress.setValue(0)
        self.upload_note.setText(f"Copying {src} to ~/{name}…")
        self.report(f"packing {src}…")
        self._update_buttons()

        def work(report):
            return self.backend.upload(url, src, name, excludes, report=report)

        def progress(sent, total):
            self.upload_progress.setValue(int(sent * 100 / total) if total else 0)
            if auto:
                self.upload_note.setText(
                    f"Copying {human_bytes(sent)} of {human_bytes(total)}…")
            else:
                self.report(f"uploading {human_bytes(sent)} of {human_bytes(total)}")

        def finish(result):
            self._uploading = False
            self.upload_progress.setValue(100)
            self.upload_note.setText(f"Copied to {result.get('path')}")
            self.report(f"copied into {result.get('path')}")
            if not auto:
                QMessageBox.information(
                    self, "Copy",
                    f"{result.get('files')} files ({human_bytes(result.get('bytes', 0))}) "
                    f"are now in {result.get('path')}.")
            self._update_buttons()

        def fail(message):
            self._uploading = False
            self.upload_note.setText(f"Copy failed: {message}")

        self.task(work, finish, title=None if auto else "Copy", progress=progress,
                  failed=fail)

    def open_desktop(self):
        url = self.session_url() or self.url_edit.text().strip()
        if url:
            self._show_desktop(url)

    def copy_url(self):
        url = self.url_edit.text().strip() or self.session_url()
        if url:
            QApplication.clipboard().setText(url)
            self.report("session URL copied")

    def open_run(self):
        url = self._run_url()
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _show_desktop(self, url):
        if not url:
            self.report("no session URL yet", error=True)
            return
        self.url_edit.setText(url)
        self.tabs.setCurrentIndex(1)
        if QWebEngineView is None:
            self.desktop_hint.setText(
                "QtWebEngine is unavailable in this build; the session was opened in "
                "your browser instead.")
            self._open_in_browser(url)
            return
        if self._desktop_view is None:
            try:
                self._desktop_view = QWebEngineView(self)
            except Exception as error:                    # missing GL, sandbox, ...
                self.desktop_hint.setText(
                    f"QtWebEngine could not start ({error}); using your browser.")
                self._open_in_browser(url)
                return
            self.desktop_holder.addWidget(self._desktop_view)
        self.desktop_hint.setText("Desktop ready — the page below is live.")
        self._desktop_view.setUrl(QUrl(url))
        self._update_buttons()

    def _open_in_browser(self, url):
        if not url:
            self.report("no session URL yet", error=True)
            return
        QDesktopServices.openUrl(QUrl(url))

    # -- dashboard ----------------------------------------------------------------

    def _refresh_dashboard(self):
        self.table.setRowCount(len(self.runs))
        highlight = self.palette().highlight().color()
        highlight.setAlpha(48)
        brush = QBrush(highlight)
        for row, run in enumerate(self.runs):
            run_id = run.get("databaseId")
            entry = self.sessions.get(str(run_id)) or {}
            status = run.get("status") or "unknown"
            conclusion = run.get("conclusion")
            completed = status == "completed"
            active = str(run_id) == str(self.active_run_id)
            if active and self._light == "ready":
                label = "ready"
            elif active and self._light in ("starting", "amber"):
                label = f"starting {self.progress_bar.value()}%"
            elif completed:
                label = conclusion or "completed"
            else:
                label = status
            started = parse_utc(run.get("startedAt") or run.get("createdAt") or "")
            values = [
                label,
                str(run_id),
                f"{entry['minutes']} min" if entry.get("minutes") else "—",
                started.astimezone().strftime("%H:%M") if started else "—",
                self._remaining(entry, completed),
                "ready" if entry.get("ready_at") else "—",
                entry.get("url") or "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if not completed:
                    item.setBackground(brush)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row, column, item)

    def _remaining(self, entry, completed=False):
        if not entry:
            return "—"
        ready = parse_utc(entry.get("ready_at") or "")
        minutes = _int(entry.get("minutes"))
        if not ready or not minutes:
            return "—"
        if completed:
            return "over"
        left = ready.timestamp() + minutes * 60 - time.time()
        if left <= 0:
            return "over"
        return f"{int(left // 60)} min"

    def _dashboard_open(self, row, _column):
        if not 0 <= row < len(self.runs):
            return
        run = self.runs[row]
        if run.get("status") == "completed":
            self.report("that session has ended")
            return
        entry = self.sessions.get(str(run.get("databaseId"))) or {}
        url = entry.get("url") or ""
        if url:
            self._show_desktop(url)
        else:
            self.report("that run was started outside this app (its URL is not stored)")

    def stop_selected_run(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self.runs):
            self.report("select a run first")
            return
        run = self.runs[row]
        if run.get("status") == "completed":
            self.report("that run has already finished")
            return
        self.cancel_run(run.get("databaseId"))

    # -- setup --------------------------------------------------------------------

    def run_checks(self):
        self.report("running setup checks")

        def done(rows):
            self.check_table.setRowCount(len(rows))
            for row, check in enumerate(rows):
                mark = QTableWidgetItem("✓" if check["ok"] else "✗")
                self.check_table.setItem(row, 0, mark)
                self.check_table.setItem(row, 1, QTableWidgetItem(check["name"]))
                detail = QTableWidgetItem(check["detail"])
                detail.setToolTip(check["detail"])
                self.check_table.setItem(row, 2, detail)
            bad = [check["name"] for check in rows if not check["ok"]]
            self.report("setup looks good" if not bad else "failing: " + ", ".join(bad))

        self.task(lambda _report: self.backend.checks(), done,
                  button=self.recheck_button)

    def set_tailscale_key(self):
        value = self.key_edit.text().strip()
        if not value:
            self.report("paste the auth key first")
            return
        self.key_edit.clear()

        def work(_report):
            self.backend.set_secret("TAILSCALE_AUTH_KEY", value)
            return "secret set"

        def done(_value):
            self.report("TAILSCALE_AUTH_KEY updated")
            self.run_checks()

        self.task(work, done, button=self.key_button)

    def _runner_changed(self):
        value = self.runner_edit.text().strip()
        if not value or value == self.backend.runner_repo:
            return
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
            self.report(f"'{value}' is not OWNER/NAME", error=True)
            self.runner_edit.setText(self.backend.runner_repo)
            return
        self.backend.runner_repo = value
        self._persist()
        self.report(f"runner: {value}")
        self.check_table.setRowCount(0)


def main():
    application = QApplication(sys.argv)
    application.setApplicationName("ios-ci sessions")
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
