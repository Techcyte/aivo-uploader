"""
This script will find every not-started instance of a task type in a company, download
the scans attached to each one, and move each task through IN_PROGRESS to complete (or
back to NOT_STARTED if a scan fails). The script requires the user to configure two
environment variables.

Requirements:
    requests
    tqdm

Environment:
    TECHCYTE_API_CLIENT_ID
    TECHCYTE_API_CLIENT_SECRET

Arguments:
    --task-type-id: the task (request) type to look for; numeric or encoded GraphQL ID
    --dest: the directory to download scans into, as <dest>/<case_number>/<task_id>/<scan_id>/
    --company-id: (optional) company to search, defaults to the API client's own company
    --overwrite: (bool, optional) re-download files that are already on disk
    --dicom-annotations: (bool, optional) include the scan's annotations in each DICOM zip
    --dry-run: (bool, optional) list matching tasks, scans and sizes without downloading
    --workers: (optional) number of scans to download at once, defaults to 4
    --host: (optional) defaults to https://api.app.techcyte.com, override if you want to download from ci or staging

How it works:
    1. POST /api/v3/token (client_credentials). If --company-id names a different company
       than the API client's own, GET /api/v3/companies/{id}/impersonation_token.
    2. Page through the `casesWithFilter` GraphQL query for cases with a NOT_STARTED task
       of the task type, and list each matching scan's regions (and their sizes) up front.
    3. Download scans --workers at a time, each with its own progress bar. A task is
       marked IN_PROGRESS (editTask) when its first scan starts; each region streams
       from GET /api/v3/regions/{region_id}/contents (DICOM arrives as a zip, with
       dicom/techcyte_annotations.dcm added when --dicom-annotations is set). When a
       task's last scan finishes, it is completed (completeTask), or reset to
       NOT_STARTED if any of its scans failed.
"""

import argparse
import base64
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.parse import urljoin

import requests
from tqdm import tqdm

CASES_PAGE_SIZE = 50
TOKEN_REFRESH_MARGIN_SECONDS = 120
# (connect, read) in seconds. The read timeout is the longest wait between bytes, not a
# limit on the whole download, so large scans still stream; a stalled connection fails.
REQUEST_TIMEOUT = (10, 120)

CASES_QUERY = """
query TaskCases($first: NonNegativeInt, $offset: NonNegativeInt, $filter: CaseFilter!) {
  casesWithFilter(first: $first, offset: $offset, filter: $filter) {
    totalCount
    edges {
      node {
        id
        caseNumber
        tasks {
          id
          taskStatus
          scanIds
          taskType { id }
        }
      }
    }
  }
}
"""

TASK_TYPES_QUERY = """
query TaskTypes($first: NonNegativeInt, $offset: NonNegativeInt) {
  taskTypes(first: $first, offset: $offset) {
    edges { node { id type } }
  }
}
"""

EDIT_TASK_MUTATION = """
mutation EditTask($input: EditTaskInput!) {
  editTask(input: $input) {
    errors { ... on Error { message } }
    task { id taskStatus }
  }
}
"""

# completeTask (rather than editTask with status COMPLETE) also evaluates the case's
# "request complete" policies, matching what completing the task in the UI does.
COMPLETE_TASK_MUTATION = """
mutation CompleteTask($input: CompleteTaskInput!) {
  completeTask(input: $input) {
    errors { ... on Error { message } }
    task { id taskStatus }
  }
}
"""


def say(message: str):
    """Print a line above any active progress bars."""
    tqdm.write(message)


def warn(message: str):
    tqdm.write(f"WARNING: {message}", file=sys.stderr)


def error(message: str):
    tqdm.write(f"ERROR: {message}", file=sys.stderr)


def encode_id(key: str, value: str) -> str:
    """Encode a raw integer ID the way techcyte's GraphQL layer does ("key:N" base64)."""
    if not value.isdigit():
        return value  # assume it's already encoded
    return base64.b64encode(f"{key}:{value}".encode()).decode()


def decode_id(encoded: str) -> str:
    """Best-effort decode of an encoded GraphQL ID to its raw integer, for display/paths."""
    try:
        return base64.b64decode(encoded).decode().split(":", 1)[1]
    except Exception:
        return encoded


def jwt_claims(token: str) -> dict:
    """Read a JWT's claims without verifying it (only used for expiry and company ID)."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def token_company_id(token: str) -> str:
    claims = jwt_claims(token)
    return str(claims.get("https://techcyte.com/company_id", claims.get("company_id")))


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_") or "unnamed"


class TechcyteSession:
    """Requests session that authenticates with client credentials and refreshes as needed."""

    def __init__(self, host: str, company_id: str | None):
        self.host = host.rstrip("/") + "/"
        self.company_id = company_id
        self._local = threading.local()
        self._auth_lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def http(self) -> requests.Session:
        """requests.Session isn't thread-safe, so each worker thread gets its own."""
        if not hasattr(self._local, "http"):
            self._local.http = requests.Session()
        return self._local.http

    def _authenticate(self):
        resp = self.http.post(
            urljoin(self.host, "api/v3/token"),
            timeout=REQUEST_TIMEOUT,
            data={
                "grant_type": "client_credentials",
                "client_id": os.environ.get("TECHCYTE_API_CLIENT_ID"),
                "client_secret": os.environ.get("TECHCYTE_API_CLIENT_SECRET"),
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]

        if self.company_id and token_company_id(token) != str(self.company_id):
            say(f"Impersonating company {self.company_id}")
            resp = self.http.get(
                urljoin(
                    self.host, f"api/v3/companies/{self.company_id}/impersonation_token"
                ),
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]

        self._token = token
        self._expires_at = float(jwt_claims(token).get("exp", time.time() + 3600))

    def _auth_header(self) -> dict:
        # Locked so that when the token expires, only one thread fetches a new one.
        with self._auth_lock:
            if (
                self._token is None
                or time.time() > self._expires_at - TOKEN_REFRESH_MARGIN_SECONDS
            ):
                self._authenticate()
            return {"Authorization": f"Bearer {self._token}"}

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = {**kwargs.pop("headers", {}), **self._auth_header()}
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)
        resp = self.http.request(
            method, urljoin(self.host, path.lstrip("/")), headers=headers, **kwargs
        )
        resp.raise_for_status()
        return resp

    def graphql(self, query: str, variables: dict) -> dict:
        body = self.request(
            "POST", "api/graphql", json={"query": query, "variables": variables}
        ).json()
        if body.get("errors"):
            raise RuntimeError(f"GraphQL errors: {body['errors']}")
        return body["data"]

    def set_task_status(self, task_id: str, status: str):
        if status == "COMPLETE":
            result = self.graphql(COMPLETE_TASK_MUTATION, {"input": {"id": task_id}})[
                "completeTask"
            ]
        else:
            result = self.graphql(
                EDIT_TASK_MUTATION, {"input": {"id": task_id, "status": status}}
            )["editTask"]
        if result.get("errors"):
            raise RuntimeError(
                f"Failed to set task {task_id} to {status}: {result['errors']}"
            )


def get_task_type_name(session: TechcyteSession, task_type_id: str) -> str | None:
    """Look up a task type's display name among the company's task types (None if absent)."""
    offset = 0
    while True:
        edges = session.graphql(
            TASK_TYPES_QUERY, {"first": CASES_PAGE_SIZE, "offset": offset}
        )["taskTypes"]["edges"]
        for edge in edges:
            if edge["node"]["id"] == task_type_id:
                return edge["node"].get("type") or "(unnamed)"
        if len(edges) < CASES_PAGE_SIZE:
            return None
        offset += CASES_PAGE_SIZE


def find_not_started_tasks(
    session: TechcyteSession, task_type_id: str
) -> list[tuple[dict, dict]]:
    """Return (case, task) for every NOT_STARTED task of the given type in the company.

    Collected up front rather than processed page by page: claiming a task drops its case
    out of the filter, which would shift the offset paging and skip cases.
    """
    case_filter = {
        "tasks": [{"taskTypes": [task_type_id], "taskStatus": ["NOT_STARTED"]}]
    }
    matches = []
    offset = 0
    while True:
        data = session.graphql(
            CASES_QUERY,
            {"first": CASES_PAGE_SIZE, "offset": offset, "filter": case_filter},
        )["casesWithFilter"]
        edges = data.get("edges") or []
        if offset == 0:
            say(
                f"{data.get('totalCount')} case(s) have a not-started task of this type"
            )
        for edge in edges:
            case = edge["node"]
            # The case filter only says *some* task matches; re-check each task here.
            for task in case.get("tasks") or []:
                if (
                    task["taskStatus"].upper() == "NOT_STARTED"
                    and (task.get("taskType") or {}).get("id") == task_type_id
                ):
                    matches.append((case, task))
        if len(edges) < CASES_PAGE_SIZE:
            return matches
        offset += CASES_PAGE_SIZE


def filename_from_response(resp: requests.Response, fallback: str) -> str:
    disposition = resp.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition)
    return safe_name(match.group(1) if match else fallback)


def list_regions(session: TechcyteSession, scan_id: str) -> list[dict]:
    """The scan's non-synthetic regions, i.e. its image files."""
    return session.request(
        "GET", f"api/v3/samples/{scan_id}/regions", params={"synthetic": "false"}
    ).json()


def region_marker(dest_dir: Path, region: dict) -> Path:
    """A marker file records finished regions, so reruns skip them without re-downloading."""
    return dest_dir / f".region_{region['id']}.done"


def pending_bytes(regions: list[dict], dest_dir: Path, overwrite: bool) -> int:
    """Bytes still to download for a scan, from the size the server records for each region.

    Regions with no recorded size count as 0 here; the bars are corrected once their real
    size is known.
    """
    return sum(
        int(r.get("file_size") or 0)
        for r in regions
        if overwrite or not region_marker(dest_dir, r).exists()
    )


def unsized_count(regions: list[dict], dest_dir: Path, overwrite: bool) -> int:
    """Regions still to download that have no recorded size (left out of the byte totals)."""
    return sum(
        1
        for r in regions
        if not r.get("file_size")
        and (overwrite or not region_marker(dest_dir, r).exists())
    )


def format_bytes(n: int) -> str:
    return tqdm.format_sizeof(n, "B", 1024)


class Cancelled(Exception):
    """Raised inside a download when the run is stopping (e.g. on Ctrl-C)."""


class DownloadRun:
    """State shared by the worker threads: overall bars, counters and bar slots."""

    def __init__(self, session: TechcyteSession, args, scans_bar: tqdm, bytes_bar: tqdm):
        self.session = session
        self.args = args
        self.scans_bar = scans_bar
        self.bytes_bar = bytes_bar
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.completed = self.files = self.failures = self.received = 0
        # Each scan being downloaded borrows a slot: a fixed line under the overall bars
        # for its own bar, so concurrent scans don't draw over each other.
        self.free_slots: queue.SimpleQueue[int] = queue.SimpleQueue()
        for slot in range(args.workers):
            self.free_slots.put(slot)

    def add_bytes(self, n: int):
        with self.lock:
            self.received += n
            self.bytes_bar.update(n)

    def finish_scan(self, received: int, expected: int):
        """Count a scan as done and correct the byte total to what actually arrived.

        Covers every outcome: a size that was missing or wrong, a scan that failed
        partway, and one that never started.
        """
        with self.lock:
            self.bytes_bar.total += received - expected
            self.bytes_bar.refresh()
            self.scans_bar.update(1)

    def count(self, completed: int = 0, files: int = 0, failures: int = 0):
        with self.lock:
            self.completed += completed
            self.files += files
            self.failures += failures


class TaskRun:
    """Claim and finish bookkeeping for one task whose scans download on several threads."""

    def __init__(self, job: dict):
        self.job = job
        self.lock = threading.Lock()
        self.pending = len(job["scans"])
        self.claimed = False
        self.claim_failed = False
        self.failed = False
        self.finalized = False


def claim_task(run: DownloadRun, state: TaskRun) -> bool:
    """Mark the task IN_PROGRESS when its first scan starts; later scans reuse the outcome."""
    with state.lock:
        if not state.claimed and not state.claim_failed:
            job = state.job
            try:
                run.session.set_task_status(job["task"]["id"], "IN_PROGRESS")
                state.claimed = True
                task_bytes = sum(scan["bytes"] for scan in job["scans"])
                say(
                    f"Case {job['case_label']} task {job['task_label']}: "
                    f"{len(job['scans'])} scan(s), {format_bytes(task_bytes)}"
                )
            except (requests.RequestException, RuntimeError) as e:
                state.claim_failed = True
                run.count(failures=1)
                error(
                    f"  could not mark task {job['task_label']} in progress; skipping: {e}"
                )
        return state.claimed


def finish_task(run: DownloadRun, state: TaskRun):
    """Complete the task, or reset it to NOT_STARTED if any of its scans failed."""
    with state.lock:
        if state.finalized or not state.claimed:
            return
        state.finalized = True
        failed = state.failed
    task, label = state.job["task"], state.job["task_label"]
    if failed:
        run.count(failures=1)
    try:
        if failed:
            warn(f"  resetting task {label} to NOT_STARTED")
            run.session.set_task_status(task["id"], "NOT_STARTED")
        else:
            run.session.set_task_status(task["id"], "COMPLETE")
            run.count(completed=1)
            say(f"  task {label} complete")
    except (requests.RequestException, RuntimeError) as e:
        if not failed:
            run.count(failures=1)
        error(f"  could not update status of task {label} (left IN_PROGRESS): {e}")


def run_scan(run: DownloadRun, state: TaskRun, scan: dict):
    """Worker: download one scan, and finish its task if it was the task's last scan."""
    received = 0
    failed = False

    def on_chunk(n: int):
        nonlocal received
        received += n
        run.add_bytes(n)

    try:
        if run.stop.is_set():
            raise Cancelled
        if claim_task(run, state):
            if "error" in scan:
                raise scan["error"]
            run.count(files=download_scan(run, scan, on_chunk))
    except Cancelled:
        failed = True
    except (requests.RequestException, OSError) as e:
        failed = True
        error(f"  scan {scan['id']} failed: {e}")
    finally:
        run.finish_scan(received, scan["bytes"])
        with state.lock:
            state.failed |= failed
            state.pending -= 1
            last = state.pending == 0
        if last:
            finish_task(run, state)


def download_all(run: DownloadRun, jobs: list[dict]):
    """Download every task's scans on run.args.workers threads."""
    states = [TaskRun(job) for job in jobs]
    pool = ThreadPoolExecutor(
        max_workers=run.args.workers, thread_name_prefix="download"
    )
    try:
        futures = []
        for state in states:
            if not state.job["scans"]:
                if claim_task(run, state):
                    finish_task(run, state)
                continue
            # Queued in task order, so only about --workers tasks are claimed at a time.
            futures += [
                pool.submit(run_scan, run, state, scan) for scan in state.job["scans"]
            ]
        # Wait in short slices: a blocking wait isn't interrupted by Ctrl-C everywhere
        # (notably Windows), which would leave Ctrl-C ignored until downloads finish.
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                future.result()
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            warn("Interrupted: stopping downloads and releasing claimed tasks")
        run.stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        # Cancelled scans never ran, so their tasks are still claimed: reset them.
        for state in states:
            with state.lock:
                state.failed = True
            finish_task(run, state)
        raise
    finally:
        pool.shutdown(wait=True)


def download_scan(run: DownloadRun, scan: dict, on_chunk) -> int:
    """Download every region of a scan into its directory. Returns files written."""
    regions, dest_dir = scan["regions"], scan["dest"]
    if not regions:
        warn(f"Scan {scan['id']} has no regions; skipping")
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    slot = run.free_slots.get()
    try:
        with tqdm(
            total=scan["bytes"],
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"scan {scan['id']}",
            position=2 + slot,
            leave=False,
        ) as scan_bar:
            return sum(
                download_region(run, region, dest_dir, scan_bar, on_chunk)
                for region in regions
            )
    finally:
        run.free_slots.put(slot)


def download_region(
    run: DownloadRun, region: dict, dest_dir: Path, scan_bar: tqdm, on_chunk
) -> int:
    """Download one region into dest_dir. Returns files written (0 or 1)."""
    region_id = region["id"]
    fallback = region.get("label") or f"region_{region_id}"
    if region.get("mimetype") == "application/dicom" and not fallback.endswith(".zip"):
        fallback += ".zip"

    marker = region_marker(dest_dir, region)
    if marker.exists() and not run.args.overwrite:
        say(f"  region {region_id} already downloaded; skipping")
        return 0

    params = {}
    if run.args.dicom_annotations and region.get("mimetype") == "application/dicom":
        params["dicom_annotations"] = "true"

    expected = int(region.get("file_size") or 0)
    received = 0
    with run.session.request(
        "GET", f"api/v3/regions/{region_id}/contents", params=params, stream=True
    ) as resp:
        out_path = dest_dir / filename_from_response(resp, fallback)
        tmp_path = out_path.with_name(out_path.name + ".part")
        try:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=2**20):
                    if run.stop.is_set():
                        raise Cancelled
                    f.write(chunk)
                    received += len(chunk)
                    scan_bar.update(len(chunk))
                    on_chunk(len(chunk))
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(out_path)
    # The recorded size can be missing or differ from what's served (e.g. a DICOM series
    # arrives zipped), so correct the scan's bar to what was actually received.
    scan_bar.total += received - expected
    scan_bar.refresh()
    marker.write_text(out_path.name)
    say(f"  region {region_id} -> {out_path}")
    return 1


def list_jobs(jobs: list[dict]):
    """Dry run: print each task's scans and sizes."""
    for job in jobs:
        task_bytes = sum(scan["bytes"] for scan in job["scans"])
        say(
            f"Case {job['case_label']} task {job['task_label']}: "
            f"{len(job['scans'])} scan(s), {format_bytes(task_bytes)}"
        )
        for scan in job["scans"]:
            if "error" in scan:
                error(f"  scan {scan['id']}: could not list its files: {scan['error']}")
            else:
                unknown = (
                    f" + {scan['unsized']} file(s) of unknown size"
                    if scan["unsized"]
                    else ""
                )
                say(f"  scan {scan['id']}: {format_bytes(scan['bytes'])}{unknown}")


def main(args) -> int:
    if not os.environ.get("TECHCYTE_API_CLIENT_ID") or not os.environ.get(
        "TECHCYTE_API_CLIENT_SECRET"
    ):
        error(
            "set the TECHCYTE_API_CLIENT_ID and TECHCYTE_API_CLIENT_SECRET environment variables"
        )
        return 1

    session = TechcyteSession(args.host, args.company_id)
    task_type_id = encode_id("taskType", args.task_type_id)
    task_type_name = get_task_type_name(session, task_type_id)
    if task_type_name is None:
        error(
            f"Task type {decode_id(task_type_id)} ({task_type_id}) not found in this company"
        )
        return 1
    say(f'Task type: "{task_type_name}" ({decode_id(task_type_id)})')

    jobs = size_jobs(session, find_not_started_tasks(session, task_type_id), args)
    total_scans = sum(len(job["scans"]) for job in jobs)
    total_bytes = sum(scan["bytes"] for job in jobs for scan in job["scans"])
    say(
        f"{len(jobs)} task(s), {total_scans} scan(s), {format_bytes(total_bytes)} to download"
    )
    unsized = sum(scan["unsized"] for job in jobs for scan in job["scans"])
    if unsized:
        warn(
            f"{unsized} file(s) have no recorded size; the byte total grows as they download"
        )

    if args.dry_run:
        list_jobs(jobs)
        return 0

    with (
        tqdm(
            total=total_scans,
            unit="scan",
            desc="All scans",
            position=0,
            disable=total_scans == 0,
        ) as scans_bar,
        tqdm(
            total=total_bytes,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc="All bytes",
            position=1,
            disable=total_scans == 0,
        ) as bytes_bar,
    ):
        run = DownloadRun(session, args, scans_bar, bytes_bar)
        download_all(run, jobs)

    say(
        f'Done ("{task_type_name}"): {len(jobs)} task(s) found, {run.completed} completed, '
        f"{run.files} file(s) downloaded ({format_bytes(run.received)}), "
        f"{run.failures} failure(s)"
    )
    return 1 if run.failures else 0


def size_jobs(session: TechcyteSession, matches, args) -> list[dict]:
    """List every scan's regions up front, so the overall bars know the total size before starting."""
    jobs = []
    for case, task in matches:
        case_label = safe_name(case.get("caseNumber") or decode_id(case["id"]))
        task_label = decode_id(task["id"])
        jobs.append(
            {
                "task": task,
                "case_label": case_label,
                "task_label": task_label,
                "scans": [
                    {
                        "id": scan_id,
                        "dest": args.dest / case_label / task_label / scan_id,
                    }
                    for scan_id in task.get("scanIds") or []
                ],
            }
        )

    all_scans = [scan for job in jobs for scan in job["scans"]]
    pool = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="size")
    try:
        sized = pool.map(lambda scan: size_scan(session, scan, args.overwrite), all_scans)
        for _ in tqdm(
            sized,
            total=len(all_scans),
            unit="scan",
            desc="Sizing scans",
            leave=False,
            disable=not all_scans,
        ):
            pass
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return jobs


def size_scan(session: TechcyteSession, scan: dict, overwrite: bool):
    """List one scan's regions and how many bytes of them are still to download."""
    try:
        scan["regions"] = list_regions(session, scan["id"])
        scan["bytes"] = pending_bytes(scan["regions"], scan["dest"], overwrite)
        scan["unsized"] = unsized_count(scan["regions"], scan["dest"], overwrite)
    except requests.RequestException as e:
        # Reported as a failure of this scan when its task is processed.
        scan["regions"], scan["bytes"], scan["unsized"], scan["error"] = None, 0, 0, e


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--task-type-id",
        help="task (request) type to look for; numeric or encoded GraphQL ID",
        required=True,
    )
    parser.add_argument(
        "--dest", help="directory to download scans into", type=Path, required=True
    )
    parser.add_argument("--host", default="https://api.app.techcyte.com")
    parser.add_argument(
        "--company-id",
        help="company to search; defaults to the API client's own company, "
        "a different company requires the client to be allowed to impersonate it",
    )
    parser.add_argument(
        "--overwrite",
        type=bool,
        default=False,
        help="re-download files that are already on disk",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "--dicom-annotations",
        type=bool,
        default=False,
        help="include the scan's annotations in each DICOM zip as dicom/techcyte_annotations.dcm",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "--dry-run",
        type=bool,
        default=False,
        help="list matching tasks, scans and sizes without downloading or changing task status",
        action=argparse.BooleanOptionalAction,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="number of scans to download at once",
    )
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    sys.exit(main(args))
