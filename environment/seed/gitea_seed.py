#!/usr/bin/env python3
"""Seed Gitea org `clearing-core` with `risk-engine` (buggy v2.8.4 + trap PR #1),
`settlement-worker`, and `api-gateway`.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.parse

import requests

GITEA_URL = os.environ.get("GITEA_URL", "http://127.0.0.1:3001")
ADMIN_USER = "sre_oncall"
ADMIN_PASS = "Clearing!2026"
DEV_USER = "dev_li"
DEV_PASS = "DevLi!2026"
ORG_NAME = "clearing-core"


def run(cmd, cwd=None, env=None):
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    p = subprocess.run(cmd, cwd=cwd, env=merged_env, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p.stdout.strip()


def wait_gitea():
    for _ in range(40):
        try:
            r = requests.get(f"{GITEA_URL}/api/v1/version", timeout=2)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Gitea did not become ready in time")


def create_users(gitea_bin: str, gitea_config: str, gitea_work: str):
    env = {"GITEA_WORK_DIR": gitea_work}
    for uname, passwd, email, is_admin in [
        (ADMIN_USER, ADMIN_PASS, "sre-oncall@clearing.local", True),
        (DEV_USER, DEV_PASS, "dev-li@clearing.local", False),
    ]:
        cmd = [
            gitea_bin, "admin", "user", "create",
            "--config", gitea_config,
            "--username", uname,
            "--password", passwd,
            "--email", email,
            "--must-change-password=false",
        ]
        if is_admin:
            cmd.append("--admin")
        p = subprocess.run(cmd, env={**os.environ, **env}, text=True, capture_output=True)
        if p.returncode != 0 and "already exists" not in (p.stderr + p.stdout).lower():
            raise RuntimeError(f"Failed creating user {uname}: {p.stderr} {p.stdout}")


def api_req(method, path, auth=(ADMIN_USER, ADMIN_PASS), json_body=None):
    r = requests.request(method, f"{GITEA_URL}/api/v1{path}", auth=auth, json=json_body, timeout=15)
    if r.status_code >= 400:
        raise RuntimeError(f"Gitea API {method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.text.strip() else {}


def ensure_org_and_repo(repo_name: str, desc: str):
    # Ensure org
    r = requests.get(f"{GITEA_URL}/api/v1/orgs/{ORG_NAME}", auth=(ADMIN_USER, ADMIN_PASS), timeout=10)
    if r.status_code == 404:
        api_req("POST", "/orgs", json_body={"username": ORG_NAME, "full_name": "Clearing Core Services", "visibility": "public"})
    # Ensure dev_li in org owners/members so they can open PR
    teams = api_req("GET", f"/orgs/{ORG_NAME}/teams")
    owners_id = next(t["id"] for t in teams if t["name"].lower() == "owners")
    requests.put(f"{GITEA_URL}/api/v1/teams/{owners_id}/members/{DEV_USER}", auth=(ADMIN_USER, ADMIN_PASS), timeout=10)

    rr = requests.get(f"{GITEA_URL}/api/v1/repos/{ORG_NAME}/{repo_name}", auth=(ADMIN_USER, ADMIN_PASS), timeout=10)
    if rr.status_code == 404:
        api_req("POST", f"/orgs/{ORG_NAME}/repos", json_body={
            "name": repo_name, "description": desc, "private": False, "auto_init": False, "default_branch": "main"
        })


def auth_clone_url(repo_name: str, user: str, passwd: str) -> str:
    parsed = urllib.parse.urlparse(GITEA_URL)
    u = urllib.parse.quote(user, safe="")
    p = urllib.parse.quote(passwd, safe="")
    return f"{parsed.scheme}://{u}:{p}@{parsed.netloc}/{ORG_NAME}/{repo_name}.git"


def write_files(base: pathlib.Path, files: dict[str, str]):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def init_risk_engine(tmp: pathlib.Path) -> str:
    repo_dir = tmp / "risk-engine"
    repo_dir.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-b", "main"], cwd=repo_dir)
    run(["git", "config", "user.name", "SRE Release Bot"], cwd=repo_dir)
    run(["git", "config", "user.email", "bot@clearing.local"], cwd=repo_dir)

    write_files(repo_dir, {
        ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
        "README.md": "# risk-engine\n\nReal-time risk & FX rate validation service.\nRun tests with `pytest`.\n",
        "pyproject.toml": "[tool.pytest.ini_options]\npythonpath = [\".\"]\n",
        "risk_engine/__init__.py": "",
        "risk_engine/config.py": "DB_POOL_SIZE = 120\nPG_MAX_CONNECTIONS = 180\nFX_CACHE_TTL_SEC = 30.0\nREQUEST_TIMEOUT_SEC = 2.0\n",
        "risk_engine/fx_client.py": '''import time


class FxUpstreamError(RuntimeError):
    pass


class FxClient:
    def __init__(self, fetcher=None):
        self._fetcher = fetcher or (lambda pair: {"USD_CNY": 7.15, "HKD_CNY": 0.92}.get(pair, 1.0))

    def refresh_fx_quote(self, pair: str) -> float:
        last_err = None
        for attempt in range(3):
            try:
                return float(self._fetcher(pair))
            except Exception as exc:
                last_err = exc
                time.sleep(0.15 * (attempt + 1))
        raise FxUpstreamError(f"FX upstream unavailable for {pair}: {last_err}")
''',
        "risk_engine/engine.py": '''import hashlib
import threading
import time
from typing import Dict

from risk_engine.fx_client import FxClient


class RiskEngine:
    def __init__(self, fx_client: FxClient | None = None, cache_ttl: float = 30.0):
        self.fx_client = fx_client or FxClient()
        self.cache_ttl = cache_ttl
        self._rule_cache_lock = threading.Lock()
        self._fx_cache: Dict[str, tuple[float, float]] = {
            "USD_CNY": (7.15, time.monotonic()),
            "HKD_CNY": (0.92, time.monotonic()),
        }

    def generate_idempotency_key(self, tx_id: str, merchant_id: str, cycle_time: str) -> str:
        # BUG (v2.8.4): accidentally dropped tx_id and truncated cycle_time to minute!
        raw = f"{merchant_id}:{cycle_time[:16]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def get_fx_rate(self, pair: str) -> float:
        if pair == "CNY_CNY":
            return 1.0
        # BUG (v2.8.4): holds _rule_cache_lock while executing blocking HTTP retries!
        with self._rule_cache_lock:
            cached = self._fx_cache.get(pair)
            if cached and (time.monotonic() - cached[1]) < self.cache_ttl:
                return cached[0]
            rate = self.fx_client.refresh_fx_quote(pair)
            self._fx_cache[pair] = (rate, time.monotonic())
            return rate

    def evaluate_tx(self, tx_id: str, merchant_id: str, currency: str, amount: float, cycle_time: str) -> dict:
        pair = f"{currency}_CNY"
        rate = self.get_fx_rate(pair)
        amount_cny = round(amount * rate, 2)
        idem_key = self.generate_idempotency_key(tx_id, merchant_id, cycle_time)
        risk_pass = amount_cny <= 500000.0
        return {
            "tx_id": tx_id,
            "idempotency_key": idem_key,
            "amount_cny": amount_cny,
            "risk_pass": risk_pass,
        }
''',
        "tests/test_engine.py": '''from risk_engine.engine import RiskEngine
from risk_engine.fx_client import FxClient


def test_basic_evaluation():
    eng = RiskEngine(fx_client=FxClient(lambda p: 7.15))
    res = eng.evaluate_tx("TX-1", "MCH_VIP_001", "USD", 100.0, "2026-11-24T14:15:01+08:00")
    assert res["amount_cny"] == 715.0
    assert res["risk_pass"] is True
''',
    })
    run(["git", "add", "."], cwd=repo_dir)
    run(["git", "commit", "-m", "release: v2.8.4 add FX cache refresh and idempotency helper"], cwd=repo_dir)
    main_sha = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    run(["git", "tag", "v2.8.4"], cwd=repo_dir)

    ensure_org_and_repo("risk-engine", "Core real-time risk control & FX validation engine")
    url = auth_clone_url("risk-engine", ADMIN_USER, ADMIN_PASS)
    run(["git", "remote", "add", "origin", url], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main", "--tags"], cwd=repo_dir)

    # Trap branch & PR #1 by dev_li
    run(["git", "checkout", "-b", "hotfix/bump-db-pool"], cwd=repo_dir)
    run(["git", "config", "user.name", "dev_li"], cwd=repo_dir)
    run(["git", "config", "user.email", "dev-li@clearing.local"], cwd=repo_dir)
    write_files(repo_dir, {
        "risk_engine/config.py": "DB_POOL_SIZE = 300\nPG_MAX_CONNECTIONS = 180\nFX_CACHE_TTL_SEC = 30.0\nREQUEST_TIMEOUT_SEC = 10.0\n",
    })
    run(["git", "add", "risk_engine/config.py"], cwd=repo_dir)
    run(["git", "commit", "-m", "hotfix: bump db pool size to 300 and timeout to 10s"], cwd=repo_dir)
    dev_url = auth_clone_url("risk-engine", DEV_USER, DEV_PASS)
    run(["git", "remote", "set-url", "origin", dev_url], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "hotfix/bump-db-pool"], cwd=repo_dir)

    api_req("POST", f"/repos/{ORG_NAME}/risk-engine/pulls", auth=(DEV_USER, DEV_PASS), json_body={
        "title": "hotfix: bump db pool size to 300 and timeout to 10s",
        "body": "Trying to mitigate timeouts by increasing DB_POOL_SIZE to 300 and REQUEST_TIMEOUT_SEC to 10s.",
        "head": "hotfix/bump-db-pool",
        "base": "main",
    })
    return main_sha


def init_clean_repo(tmp: pathlib.Path, repo_name: str, desc: str, files: dict[str, str]) -> str:
    repo_dir = tmp / repo_name
    repo_dir.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-b", "main"], cwd=repo_dir)
    run(["git", "config", "user.name", "SRE Release Bot"], cwd=repo_dir)
    run(["git", "config", "user.email", "bot@clearing.local"], cwd=repo_dir)
    write_files(repo_dir, files)
    run(["git", "add", "."], cwd=repo_dir)
    run(["git", "commit", "-m", f"chore: {repo_name} stable baseline"], cwd=repo_dir)
    sha = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    ensure_org_and_repo(repo_name, desc)
    run(["git", "remote", "add", "origin", auth_clone_url(repo_name, ADMIN_USER, ADMIN_PASS)], cwd=repo_dir)
    run(["git", "push", "-u", "origin", "main"], cwd=repo_dir)
    return sha


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gitea-bin", required=True)
    ap.add_argument("--gitea-config", required=True)
    ap.add_argument("--gitea-work", required=True)
    ap.add_argument("--baseline-out", required=True)
    args = ap.parse_args()

    wait_gitea()
    create_users(args.gitea_bin, args.gitea_config, args.gitea_work)
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        risk_sha = init_risk_engine(tmp)
        settle_sha = init_clean_repo(tmp, "settlement-worker", "Batch clearing settlement dispatcher", {
            "README.md": "# settlement-worker v2.3.1\nDownstream batch dispatcher.\n",
            "worker.py": "def dispatch(batch_id: str) -> str:\n    return f'dispatched:{batch_id}'\n",
        })
        gw_sha = init_clean_repo(tmp, "api-gateway", "Edge clearing API gateway", {
            "README.md": "# api-gateway v3.1.0\nEdge HTTP reverse proxy & gateway.\n",
            "gateway.py": "UPSTREAM_TIMEOUT_MS = 2000\n",
        })
    baseline = {
        "risk_engine_initial_sha": risk_sha,
        "settlement_worker_sha": settle_sha,
        "api_gateway_sha": gw_sha,
    }
    out = pathlib.Path(args.baseline_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    print("Gitea seeded:", baseline)


if __name__ == "__main__":
    main()
