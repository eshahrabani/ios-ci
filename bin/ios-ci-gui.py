#!/usr/bin/env python3
"""Desktop app for ios-ci manual macOS sessions.

Start a session, watch it come up, drive the desktop in a tab, upload a directory
into the VM's home, and stop the run when the work is done. Everything the app does
goes through `gh` and `git`, so it needs the same tools as bin/ios-ci.sh and a
tailnet login (the desktop is served over the tailnet).

    bin/ios-ci-gui                       # first run creates a venv with PySide6
    XDG_CONFIG_HOME=... bin/ios-ci-gui   # settings.json lives under that directory
"""
import http.client
import json
import os
import re
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
    QComboBox,
    QDialog,
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
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:                                  # QtWebEngine ships with PySide6-Addons
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:                   # a minimal wheel without the addons
    QWebEngineView = None

RUNNER_DEFAULT = os.environ.get("IOS_CI_RUNNER", "eshahrabani/ios-ci")
WORKFLOW = "ios-ci.yml"
CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG_DIR = os.path.join(CONFIG_HOME, "ios-ci-gui")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
DEFAULT_EXCLUDES = (".git", "node_modules", "build", "DerivedData", "Pods", ".gradle")
UPLOAD_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
UPLOAD_LIMIT = 2 * 1024 ** 3          # the VM holds no more than this comfortably

DEFAULT_SETTINGS = {
    "app_dir": "",
    "runner_repo": RUNNER_DEFAULT,
    "minutes": 30,
    "node_version": "22",
    "xcode": "/Applications/Xcode_26.6.app",
    "simulator_device": "iPhone 17 Pro",
    "simulator_runtime": "",
    "platform": "",
    "brew": "",
    "setup_script": "",
    "upload_dir": "",
    "exclude_defaults": True,
    "last_run_id": None,
    "xcode_options": [],
}


def load_settings():
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


def parse_utc(text):
    """Parse the workflow's 2024-01-02T03:04:05Z timestamps."""
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class BackendError(Exception):
    pass


def require_tool(name):
    path = shutil.which(name)
    if not path:
        raise BackendError(f"{name} is not on PATH")
    return path


class Backend:
    """All gh/git/network work, with no Qt dependency."""

    def __init__(self, runner_repo=RUNNER_DEFAULT):
        self.runner_repo = (runner_repo or RUNNER_DEFAULT).strip()
        self._ready = {}               # (repo, run_id) -> parsed ready file
        self.source = os.path.dirname(os.path.abspath(__file__))

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

    def git(self, app_dir, *args, timeout=60):
        try:
            result = subprocess.run(["git", "-C", app_dir, *args],
                                    capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise BackendError("git is not installed")
        except subprocess.TimeoutExpired:
            raise BackendError(f"git {args[0]} timed out")
        if result.returncode:
            raise BackendError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout

    # -- repository identity ------------------------------------------------------

    def app_repo(self, app_dir):
        url = self.git(app_dir, "remote", "get-url", "origin").strip()
        for pattern in (r"^git@([^:]+):", r"^ssh://git@([^/]+)/", r"^https?://([^/]+)/"):
            url = re.sub(pattern, r"\1/", url)
        if url.endswith(".git"):
            url = url[:-4]
        parts = [part for part in url.split("/") if part]
        if len(parts) < 3:
            raise BackendError(f"could not read OWNER/NAME from {url}")
        return f"{parts[-2]}/{parts[-1]}"

    # -- setup checks -------------------------------------------------------------

    def checks(self, app_dir):
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
            return ", ".join(require_tool(tool) for tool in ("git", "jq"))

        def secrets():
            output = self.gh("secret", "list", "--repo", self.runner_repo, "--json", "name")
            have = {item["name"] for item in json.loads(output or "[]")}
            missing = [name for name in ("APP_REPO", "APP_DEPLOY_KEY", "TAILSCALE_AUTH_KEY")
                       if name not in have]
            if missing:
                raise BackendError("missing: " + ", ".join(missing))
            return "APP_REPO, APP_DEPLOY_KEY, TAILSCALE_AUTH_KEY"

        def app():
            if not app_dir:
                raise BackendError("no app directory selected")
            if not os.path.isdir(app_dir):
                raise BackendError(f"{app_dir} does not exist")
            self.git(app_dir, "rev-parse", "--git-dir")
            repo = self.app_repo(app_dir)
            branches = self.git(app_dir, "branch", "-r", "--contains", "HEAD").strip()
            if not branches:
                raise BackendError("HEAD is not on a remote branch; push it first")
            head = self.git(app_dir, "rev-parse", "--short", "HEAD").strip()
            return f"{repo} @ {head} ({branches.splitlines()[0].strip()})"

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
            add("git and jq", tools),
            add(f"runner secrets on {self.runner_repo}", secrets),
            add("app repository", app),
            add("tailnet", tailnet),
        ]

    # -- dispatch and runs --------------------------------------------------------

    def _newest_run(self):
        output = self.gh("run", "list", "--repo", self.runner_repo, "--workflow", WORKFLOW,
                         "--limit", "1", "--json", "databaseId")
        runs = json.loads(output or "[]")
        return int(runs[0]["databaseId"]) if runs else 0

    def dispatch(self, app_dir, minutes, node_version, boot_config):
        """Dispatch one manual run and return its id once GitHub lists it."""
        app = self.app_repo(app_dir)
        sha = self.git(app_dir, "rev-parse", "HEAD").strip()
        ref = self.git(app_dir, "rev-parse", "--abbrev-ref", "HEAD").strip() or "main"
        correlation = f"gui-{int(time.time())}-{os.getpid()}"
        smoke = "-"
        package = os.path.join(app_dir, "package.json")
        if os.path.isfile(package):
            try:
                with open(package, "r", encoding="utf-8") as handle:
                    smoke = str(json.load(handle).get("name") or "-")
            except (OSError, ValueError):
                pass
        try:
            baseline = self._newest_run()
        except BackendError:
            baseline = 0

        print(f"dispatching {app} @ {sha[:7]} ({ref}) for {minutes} min", flush=True)
        self.gh("workflow", "run", WORKFLOW, "--repo", self.runner_repo, "--ref", "main",
                "-f", f"sha={sha}", "-f", f"ref={ref}",
                "-f", f"correlation_id={correlation}", "-f", f"smoke_text={smoke}",
                "-f", "debug_public_logs=false", "-f", f"manual_minutes={minutes}",
                "-f", f"node_version={node_version}",
                "-f", f"boot_config={json.dumps(boot_config, separators=(',', ':'))}")

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
            time.sleep(5)
        raise BackendError("the dispatched run did not appear within 5 minutes")

    def runs(self, limit=20):
        output = self.gh("run", "list", "--repo", self.runner_repo, "--workflow", WORKFLOW,
                         "--limit", str(limit),
                         "--json", "databaseId,displayTitle,status,conclusion,"
                                   "createdAt,startedAt,url")
        return json.loads(output or "[]")

    def _content(self, repo, path):
        return self.gh("api", f"repos/{repo}/contents/{path}?ref=ci-logs",
                       "-H", "Accept: application/vnd.github.raw", timeout=60)

    def ready(self, repo, run_id):
        """The parsed per-run ready file, or None when this run has not written one."""
        key = (repo, str(run_id))
        if key in self._ready:
            return self._ready[key]
        try:
            text = self._content(repo, f"ci-logs/sessions/{run_id}/manual-ready.txt")
        except BackendError:
            return None
        fields = {}
        for line in text.splitlines():
            name, separator, value = line.partition(":")
            if separator:
                fields[name.strip()] = value.strip()
        if fields.get("run_id") != str(run_id) or not fields.get("desktop_url"):
            return None
        self._ready[key] = fields
        return fields

    def boot_log(self, repo, run_id):
        try:
            return self._content(repo, f"ci-logs/sessions/{run_id}/boot-config.log")
        except BackendError:
            return self._content(repo, "ci-logs/boot-config.log")

    def cancel(self, run_id):
        self.gh("run", "cancel", str(run_id), "--repo", self.runner_repo)

    # -- runner setup -------------------------------------------------------------

    def run_script(self, command, app_dir):
        """Run `bin/ios-ci.sh <command> <app_dir>`; returns (ok, combined output)."""
        script = os.path.join(self.source, "ios-ci.sh")
        if not os.path.isfile(script):
            return False, f"missing {script}"
        try:
            result = subprocess.run(["bash", script, command, app_dir],
                                    capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, str(error)
        return result.returncode == 0, (result.stdout + result.stderr).strip()

    def configure(self, app_dir):
        return self.run_script("configure", app_dir)

    def prepare(self, app_dir):
        return self.run_script("prepare", app_dir)

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
            raise BackendError("nothing to upload: the directory has no files")
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
        port = parsed.port or 80
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
            raise BackendError(f"upload failed ({status}): {body.strip()[:300]}")
        try:
            result = json.loads(body)
        except ValueError:
            raise BackendError(f"unexpected reply: {body.strip()[:200]}")
        result["sent"] = total
        result["count"] = count
        return result


class Worker(QThread):
    """One background job; functions receive report(sent, total) for progress."""

    # Not `finished`: QThread already defines that signal, and shadowing it in a
    # subclass leaves run() unconnected to the slots below.
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
        self.active_run_id = self.settings.get("last_run_id")
        self.runs = []
        self.ready_cache = {}
        self._workers = set()
        self._polling = False
        self._desktop_view = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._persist)

        self.setWindowTitle("ios-ci sessions")
        self.resize(1040, 780)
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
        self.poll_timer.setInterval(3000)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start()
        QTimer.singleShot(200, self.poll)

    # -- construction -------------------------------------------------------------

    def _build_session_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)

        row = QHBoxLayout()
        row.addWidget(QLabel("App directory"))
        self.app_edit = QLineEdit(self.settings["app_dir"])
        self.app_edit.setPlaceholderText("~/git/my-app")
        self.app_edit.editingFinished.connect(self._touch)
        row.addWidget(self.app_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_app)
        row.addWidget(browse)
        outer.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Minutes"))
        self.minutes = QSpinBox()
        self.minutes.setRange(1, 300)
        self.minutes.setValue(int(self.settings["minutes"] or 30))
        self.minutes.valueChanged.connect(self._touch)
        row.addWidget(self.minutes)
        row.addStretch(1)
        self.start_button = QPushButton("Start session")
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.start_session)
        row.addWidget(self.start_button)
        outer.addLayout(row)

        boot = QGroupBox("Boot config")
        form = QFormLayout(boot)
        self.xcode = QComboBox()
        self.xcode.setEditable(True)
        self.xcode.addItems(self.settings["xcode_options"] or [])
        self.xcode.setCurrentText(self.settings["xcode"])
        self.xcode.currentTextChanged.connect(self._touch)
        form.addRow("Xcode", self.xcode)
        self.device = QComboBox()
        self.device.setEditable(True)
        self.device.setCurrentText(self.settings["simulator_device"])
        self.device.currentTextChanged.connect(self._touch)
        form.addRow("Simulator device", self.device)
        self.runtime = QComboBox()
        self.runtime.setEditable(True)
        self.runtime.setCurrentText(self.settings["simulator_runtime"])
        self.runtime.currentTextChanged.connect(self._touch)
        form.addRow("Simulator runtime", self.runtime)
        self.platform = QLineEdit(self.settings["platform"])
        self.platform.setPlaceholderText("latest, or a build version (empty keeps the image's)")
        self.platform.editingFinished.connect(self._touch)
        form.addRow("iOS platform", self.platform)
        self.node = QComboBox()
        self.node.setEditable(True)
        self.node.addItems(["22", "24", "20"])
        self.node.setCurrentText(str(self.settings["node_version"]))
        self.node.currentTextChanged.connect(self._touch)
        form.addRow("Node version", self.node)
        self.brew = QLineEdit(self.settings["brew"])
        self.brew.setPlaceholderText("space- or comma-separated formulae, e.g. jq watch")
        self.brew.editingFinished.connect(self._touch)
        form.addRow("Homebrew", self.brew)
        self.setup_script = QPlainTextEdit(self.settings["setup_script"])
        self.setup_script.setPlaceholderText("bash, run inside the VM before the desktop opens")
        self.setup_script.setFixedHeight(84)
        self.setup_script.textChanged.connect(self._touch)
        form.addRow("Setup script", self.setup_script)
        note = QLabel("The setup script is visible in the public run — do not paste secrets.")
        note.setWordWrap(True)
        form.addRow("", note)
        outer.addWidget(boot)

        upload = QGroupBox("Upload")
        up_form = QFormLayout(upload)
        row = QHBoxLayout()
        self.upload_edit = QLineEdit(self.settings["upload_dir"])
        self.upload_edit.setPlaceholderText("local directory to copy into the session")
        self.upload_edit.editingFinished.connect(self._touch)
        row.addWidget(self.upload_edit, 1)
        upload_browse = QPushButton("Browse…")
        upload_browse.clicked.connect(self._browse_upload)
        row.addWidget(upload_browse)
        up_form.addRow("Directory", row)
        row = QHBoxLayout()
        self.upload_name = QLineEdit(os.path.basename(self.settings["upload_dir"].rstrip("/")))
        self.upload_name.setPlaceholderText("target name in the VM's home")
        self.upload_name.textChanged.connect(self._upload_note_changed)
        self.upload_name.textChanged.connect(self._touch)
        row.addWidget(self.upload_name, 1)
        self.upload_button = QPushButton("Upload")
        self.upload_button.clicked.connect(self.upload)
        row.addWidget(self.upload_button)
        up_form.addRow("Target name", row)
        self.exclude_box = QCheckBox("Skip heavy folders (.git, node_modules, build, "
                                     "DerivedData, Pods, .gradle)")
        self.exclude_box.setChecked(bool(self.settings["exclude_defaults"]))
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

        status = QGroupBox("Session")
        grid = QGridLayout(status)
        grid.addWidget(QLabel("State"), 0, 0)
        self.state_label = QLabel("no session")
        self.state_label.setWordWrap(True)
        grid.addWidget(self.state_label, 0, 1, 1, 3)
        grid.addWidget(QLabel("Run"), 1, 0)
        self.run_label = QLabel("—")
        grid.addWidget(self.run_label, 1, 1, 1, 3)
        grid.addWidget(QLabel("Time"), 2, 0)
        self.time_label = QLabel("—")
        grid.addWidget(self.time_label, 2, 1, 1, 3)
        grid.addWidget(QLabel("URL"), 3, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.url_edit.setPlaceholderText("published once the desktop is ready")
        grid.addWidget(self.url_edit, 3, 1, 1, 2)
        row = QHBoxLayout()
        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self.open_desktop)
        row.addWidget(self.open_button)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy_url)
        row.addWidget(self.copy_button)
        grid.addLayout(row, 3, 3)
        row = QHBoxLayout()
        self.stop_button = QPushButton("Stop session")
        self.stop_button.clicked.connect(lambda: self.cancel_run(self.active_run_id))
        row.addWidget(self.stop_button)
        self.boot_log_button = QPushButton("Boot log")
        self.boot_log_button.clicked.connect(self.show_boot_log)
        row.addWidget(self.boot_log_button)
        row.addStretch(1)
        grid.addLayout(row, 4, 0, 1, 4)
        outer.addWidget(status)
        outer.addStretch(1)
        self.tabs.addTab(tab, "Session")

    def _build_desktop_tab(self):
        self.desktop_tab = QWidget()
        layout = QVBoxLayout(self.desktop_tab)
        row = QHBoxLayout()
        self.desktop_hint = QLabel(
            "The desktop opens here after the session is ready. "
            + ("" if QWebEngineView else "QtWebEngine is unavailable in this build."))
        self.desktop_hint.setWordWrap(True)
        row.addWidget(self.desktop_hint, 1)
        self.desktop_browser_button = QPushButton("Open in browser")
        self.desktop_browser_button.clicked.connect(
            lambda: self._open_in_browser(self.url_edit.text().strip()))
        row.addWidget(self.desktop_browser_button)
        layout.addLayout(row)
        self.desktop_holder = QVBoxLayout()
        layout.addLayout(self.desktop_holder, 1)
        self.tabs.addTab(self.desktop_tab, "Desktop")

    def _build_dashboard_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Recent runs (double-click a row to open its desktop)"))
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
            ["Status", "Run", "Session", "Started", "Remaining", "App", "URL"])
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
        self.configure_button = QPushButton("Configure app repo")
        self.configure_button.clicked.connect(lambda: self._runner_script("configure"))
        row.addWidget(self.configure_button)
        self.prepare_button = QPushButton("Add smoke flow")
        self.prepare_button.clicked.connect(lambda: self._runner_script("prepare"))
        row.addWidget(self.prepare_button)
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
        """Restore the last session and the values seen in earlier ready files."""
        options = list(self.settings.get("xcode_options") or [])
        for path in options:
            if self.xcode.findText(path) < 0:
                self.xcode.addItem(path)
        if self.active_run_id:
            self.set_state(f"reconnecting to run {self.active_run_id}")
            self.run_label.setText(str(self.active_run_id))
        self._update_buttons()

    # -- settings -----------------------------------------------------------------

    def _touch(self):
        self._save_timer.start()

    def _settings_from_widgets(self):
        return {
            "app_dir": self.app_edit.text().strip(),
            "runner_repo": self.backend.runner_repo,
            "minutes": self.minutes.value(),
            "node_version": self.node.currentText().strip(),
            "xcode": self.xcode.currentText().strip(),
            "simulator_device": self.device.currentText().strip(),
            "simulator_runtime": self.runtime.currentText().strip(),
            "platform": self.platform.text().strip(),
            "brew": self.brew.text().strip(),
            "setup_script": self.setup_script.toPlainText(),
            "upload_dir": self.upload_edit.text().strip(),
            "exclude_defaults": self.exclude_box.isChecked(),
            "last_run_id": self.active_run_id,
            "xcode_options": self._xcode_options(),
        }

    def _xcode_options(self):
        values = [self.xcode.itemText(index) for index in range(self.xcode.count())]
        current = self.xcode.currentText().strip()
        if current and current not in values:
            values.append(current)
        return [value for value in values if value]

    def _persist(self):
        self.settings.update(self._settings_from_widgets())
        save_settings(self.settings)

    def closeEvent(self, event):
        self.poll_timer.stop()
        self._persist()
        for worker in list(self._workers):
            worker.wait(2000)
        super().closeEvent(event)

    # -- small widgets ------------------------------------------------------------

    def set_state(self, text):
        self.state_label.setText(text)

    def report(self, text, error=False):
        self.statusBar().showMessage(("error: " if error else "") + text, 15000)

    def _update_buttons(self):
        has_url = bool(self.url_edit.text().strip())
        self.open_button.setEnabled(has_url)
        self.copy_button.setEnabled(has_url)
        self.upload_button.setEnabled(has_url)
        self.stop_button.setEnabled(bool(self.active_run_id))

    def _upload_note_changed(self):
        name = self.upload_name.text().strip() or "…"
        self.upload_note.setText(f"replaces ~/{name} inside the session")

    def _browse_app(self):
        path = QFileDialog.getExistingDirectory(self, "App directory",
                                                self.app_edit.text() or os.path.expanduser("~"))
        if path:
            self.app_edit.setText(path)
            self._persist()

    def _tab_changed(self, index):
        if index == 3 and self.check_table.rowCount() == 0:
            self.run_checks()

    def _browse_upload(self):
        path = QFileDialog.getExistingDirectory(self, "Directory to upload",
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

    def task(self, fn, done, button=None, title=None, progress=None):
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
            self._update_buttons()
            self.report(message, error=True)
            if title:
                QMessageBox.warning(self, title, message)

        worker.result.connect(finish)
        worker.failed.connect(fail)
        worker.start()
        return worker

    # -- dispatch -----------------------------------------------------------------

    def build_boot_config(self):
        """Only the fields the tester filled in; the workflow applies the rest."""
        config = {}
        xcode = self.xcode.currentText().strip()
        if xcode:
            config["xcode"] = xcode
        simulator = {}
        device = self.device.currentText().strip()
        if device:
            simulator["device"] = device
        runtime = self.runtime.currentText().strip()
        if runtime:
            simulator["runtime"] = runtime
        platform = self.platform.text().strip()
        if platform:
            simulator["platform"] = platform
        if simulator:
            config["simulator"] = simulator
        brew = [item for item in re.split(r"[,\s]+", self.brew.text().strip()) if item]
        if brew:
            config["brew"] = brew
        script = self.setup_script.toPlainText().strip()
        if script:
            config["script"] = script
        return config

    def start_session(self):
        app_dir = self.app_edit.text().strip()
        if not app_dir or not os.path.isdir(app_dir):
            QMessageBox.warning(self, "Start session", "Choose the app repository first.")
            return
        node = self.node.currentText().strip() or "22"
        config = self.build_boot_config()
        minutes = self.minutes.value()
        self.set_state("dispatching…")
        self.report("dispatching a manual session")

        def work(_report):
            return self.backend.dispatch(app_dir, minutes, node, config)

        def done(run_id):
            self.active_run_id = int(run_id)
            self.url_edit.clear()
            self.run_label.setText(str(run_id))
            self.set_state("queued; waiting for the desktop")
            self.report(f"dispatched run {run_id}")
            self._persist()
            self._update_buttons()
            self.poll()

        self.task(work, done, button=self.start_button, title="Start session")

    def cancel_run(self, run_id):
        if not run_id:
            return
        self.set_state(f"stopping run {run_id}…")

        def done(_value):
            self.report(f"asked GitHub to cancel run {run_id}")
            self.poll()

        self.task(lambda _report: self.backend.cancel(run_id), done)

    def upload(self):
        url = self.url_edit.text().strip()
        if not url:
            self.report("no session desktop URL yet", error=True)
            return
        src_dir = self.upload_edit.text().strip()
        if not src_dir or not os.path.isdir(src_dir):
            QMessageBox.warning(self, "Upload", "Choose a directory to upload.")
            return
        name = self.upload_name.text().strip()
        excludes = DEFAULT_EXCLUDES if self.exclude_box.isChecked() else ()
        self.upload_progress.setValue(0)
        self.report(f"packing {src_dir}…")

        def work(report):
            return self.backend.upload(url, src_dir, name, excludes, report=report)

        def progress(sent, total):
            self.upload_progress.setValue(int(sent * 100 / total) if total else 0)
            self.report(f"uploading {human_bytes(sent)} of {human_bytes(total)}")

        def done(result):
            self.upload_progress.setValue(100)
            self.report(f"uploaded into {result.get('path')}")
            QMessageBox.information(
                self, "Upload",
                f"{result.get('files')} files ({human_bytes(result.get('bytes', 0))}) "
                f"are now in {result.get('path')}.")

        self.task(work, done, button=self.upload_button, title="Upload",
                  progress=progress)

    # -- polling ------------------------------------------------------------------

    def poll(self):
        if self._polling:
            return
        self._polling = True
        app_dir = self.app_edit.text().strip()
        active = self.active_run_id

        def work(_report):
            runs = self.backend.runs()
            ready = {}
            repo = None
            if app_dir:
                try:
                    repo = self.backend.app_repo(app_dir)
                except BackendError:
                    repo = None
            if repo:
                for run in runs[:8]:
                    run_id = run.get("databaseId")
                    if run.get("status") == "completed" and run_id != active:
                        continue
                    try:
                        info = self.backend.ready(repo, run_id)
                    except BackendError:
                        info = None
                    if info:
                        ready[run_id] = info
            return runs, ready

        def done(result):
            self._workers.discard(worker)
            self._polling = False
            runs, ready = result
            self.runs = runs
            for run_id, info in ready.items():
                self.ready_cache[run_id] = info
                path = info.get("xcode")
                if path and self.xcode.findText(path) < 0:
                    self.xcode.addItem(path)
            self._refresh_dashboard()
            self._update_active(runs)
            self._update_buttons()

        def fail(message):
            self._workers.discard(worker)
            self._polling = False
            self.report(message, error=True)

        worker = Worker(work, self)
        self._workers.add(worker)
        worker.result.connect(done)
        worker.failed.connect(fail)
        worker.start()

    def _update_active(self, runs):
        if not self.active_run_id:
            self._update_session_labels(None, None)
            return
        run = next((item for item in runs
                    if str(item.get("databaseId")) == str(self.active_run_id)), None)
        info = self.ready_cache.get(self.active_run_id) or self.ready_cache.get(
            str(self.active_run_id))
        if run is None:
            self.set_state(f"waiting for run {self.active_run_id} to appear")
            self._update_session_labels(None, info)
            return
        status = run.get("status") or "unknown"
        conclusion = run.get("conclusion")
        if info:
            self.set_state(f"desktop ready — {status}"
                           + (f" ({conclusion})" if conclusion else ""))
        elif status == "completed":
            self.set_state(f"run {self.active_run_id} finished: {conclusion or 'completed'}")
        else:
            self.set_state(f"{status}; waiting for the desktop")
        if status == "completed":
            # The URL stays in the field (the VM may still be up), but there is
            # nothing left to stop.
            self.active_run_id = None
            self._persist()
        self._update_session_labels(run, info)

    def _update_session_labels(self, run, info):
        if info:
            self.url_edit.setText(info.get("desktop_url", ""))
            started = parse_utc(info.get("ready_at", ""))
            minutes = int(info.get("session_minutes") or 0)
            if started and minutes:
                end = started.timestamp() + minutes * 60
                left = end - time.time()
                elapsed = int((time.time() - started.timestamp()) // 60)
                until = datetime.fromtimestamp(end).strftime("%H:%M")
                if left <= 0:
                    self.time_label.setText(
                        f"~{until} (session over), {elapsed} min elapsed")
                else:
                    self.time_label.setText(
                        f"~{until} ({int(left // 60)} min left), {elapsed} min elapsed")
            else:
                self.time_label.setText("—")
            extras = []
            if info.get("simulator") and info["simulator"] != "none":
                extras.append(info["simulator"])
            if info.get("boot_status"):
                extras.append(f"boot {info['boot_status']}")
            self.run_label.setText(
                f"{self.active_run_id or info.get('run_id')}  " + "  ".join(extras))
        elif run:
            started = parse_utc(run.get("startedAt", "") or run.get("createdAt", ""))
            if started:
                self.time_label.setText(
                    f"started {started.astimezone().strftime('%H:%M')}, no desktop yet")
            self.run_label.setText(str(run.get("databaseId")))
        else:
            self.time_label.setText("—")

    # -- dashboard ----------------------------------------------------------------

    def _refresh_dashboard(self):
        self.table.setRowCount(len(self.runs))
        highlight = self.palette().highlight().color()
        highlight.setAlpha(48)
        brush = QBrush(highlight)
        for row, run in enumerate(self.runs):
            run_id = run.get("databaseId")
            info = self.ready_cache.get(run_id)
            status = run.get("status") or "unknown"
            conclusion = run.get("conclusion")
            label = status if status != "completed" else (conclusion or "completed")
            started = parse_utc(run.get("startedAt") or run.get("createdAt") or "")
            values = [
                label,
                str(run_id),
                f"{info.get('session_minutes')} min" if info else "—",
                started.astimezone().strftime("%H:%M") if started else "—",
                self._remaining(info),
                (info or {}).get("repo", "").rstrip("/").split("/")[-1] or "—",
                (info or {}).get("desktop_url", ""),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if status != "completed":
                    item.setBackground(brush)
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.table.setItem(row, column, item)

    def _remaining(self, info):
        if not info:
            return "—"
        started = parse_utc(info.get("ready_at", ""))
        minutes = int(info.get("session_minutes") or 0)
        if not started or not minutes:
            return "—"
        left = started.timestamp() + minutes * 60 - time.time()
        if left <= 0:
            return "over"
        return f"{int(left // 60)} min"

    def _dashboard_open(self, row, _column):
        item = self.table.item(row, 6)
        url = item.text() if item else ""
        if url:
            self._show_desktop(url)
        else:
            self.report("that run has no published desktop URL yet")

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

    # -- desktop ------------------------------------------------------------------

    def open_desktop(self):
        url = self.url_edit.text().strip()
        if url:
            self._show_desktop(url, remember=True)

    def copy_url(self):
        url = self.url_edit.text().strip()
        if url:
            QApplication.clipboard().setText(url)
            self.report("session URL copied")

    def _show_desktop(self, url, remember=False):
        if not url:
            return
        if remember:
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
        self.desktop_hint.setText("Live session — click and type in the page below.")
        self._desktop_view.setUrl(QUrl(url))
        self._update_buttons()

    def _open_in_browser(self, url):
        if not url:
            self.report("no session URL yet")
            return
        QDesktopServices.openUrl(QUrl(url))

    # -- setup --------------------------------------------------------------------

    def run_checks(self):
        app_dir = self.app_edit.text().strip()
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

        self.task(lambda _report: self.backend.checks(app_dir), done,
                  button=self.recheck_button)

    def _runner_script(self, command):
        app_dir = self.app_edit.text().strip()
        if not app_dir or not os.path.isdir(app_dir):
            QMessageBox.warning(self, "Setup", "Choose the app repository first.")
            return
        self.report(f"running ios-ci.sh {command}")
        button = self.configure_button if command == "configure" else self.prepare_button

        def work(_report):
            return self.backend.run_script(command, app_dir)

        def done(result):
            ok, output = result
            self._show_text(f"ios-ci.sh {command}", output or f"(no output, ok={ok})")
            self.report(f"ios-ci.sh {command}: {'ok' if ok else 'failed'}", error=not ok)
            self.run_checks()

        self.task(work, done, button=button)

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

    def show_boot_log(self):
        run_id = self.active_run_id
        if not run_id:
            self.report("no active run")
            return
        app_dir = self.app_edit.text().strip()

        def work(_report):
            repo = self.backend.app_repo(app_dir)
            return self.backend.boot_log(repo, run_id)

        self.task(work, lambda text: self._show_text(f"Boot log — run {run_id}", text),
                  button=self.boot_log_button, title="Boot log")

    def _show_text(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(760, 520)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        layout.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        dialog.exec()


def main():
    application = QApplication(sys.argv)
    application.setApplicationName("ios-ci sessions")
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
