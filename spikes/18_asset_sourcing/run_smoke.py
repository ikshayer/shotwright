"""Live, low-resolution provider sourcing smoke test (no render or publication)."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import io
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from orchestrator.api.app import create_app
from orchestrator.api.cli import main as cli_main
from orchestrator.domain.entities import Asset
from orchestrator.platform.containment import ProjectRoot
from orchestrator.platform.settings import Settings
from orchestrator.store.json_store import ProjectStore

PH_ASSETS = (
    {"source_asset_id": "wooden_table_02", "asset_id": "ph_wooden_table_02",
     "asset_type": "model", "resolution": "1k", "format": "auto"},
    {"source_asset_id": "brown_planks_03", "asset_id": "ph_brown_planks_03",
     "asset_type": "texture", "resolution": "1k", "map": "Diffuse", "format": "jpg"},
    {"source_asset_id": "studio_small_09", "asset_id": "ph_studio_small_09",
     "asset_type": "hdri", "resolution": "1k", "format": "hdr"},
)
SKETCHFAB_TOKEN_FILE = Path(r"D:\Work\blender_refs\sketchfab_key.txt")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_error_code(stderr: str) -> str:
    try:
        payload = json.loads(stderr)
    except json.JSONDecodeError:
        return "operation_error"
    value = payload.get("code") or payload.get("error_code") or payload.get("error")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) else "operation_error"


def safe_search_result(value: dict[str, Any]) -> dict[str, Any]:
    results = []
    for item in value.get("results", []):
        results.append({key: item.get(key) for key in (
            "provider", "source_asset_id", "name", "asset_type", "source_url", "license",
            "attribution", "attribution_surface_required")})
    return {key: value.get(key) for key in ("provider", "credit", "credit_url", "page", "has_more")} | {
        "results": results}


class SmokeRun:
    def __init__(self, run_id: str, with_sketchfab: bool) -> None:
        self.root = ProjectRoot(REPO)
        self.run_rel = f"spikes/18_asset_sourcing/_work/{run_id}"
        if self.root.resolve(self.run_rel).exists():
            raise FileExistsError(self.run_rel)
        self.root.make_dir(self.run_rel)
        self.root.make_dir(f"{self.run_rel}/steps")
        self.project_rel = f"{self.run_rel}/projects"
        self.root.make_dir(self.project_rel)
        self.project_id = "asset_sourcing"
        self.project_root = self.root.resolve(f"{self.project_rel}/{self.project_id}")
        self.settings = Settings(
            project_root=self.root.resolve(self.project_rel),
            **({"asset_providers": {"sketchfab_token_file": SKETCHFAB_TOKEN_FILE}}
               if with_sketchfab else {}),
            credentials={},
        )
        self.store: ProjectStore | None = None
        self.steps: list[dict[str, Any]] = []
        self.last_operation = "initialization"
        self.asset_evidence: list[dict[str, Any]] = []

    def write_json(self, relative: str, payload: Any) -> None:
        self.root.write_text(relative, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")

    def cli(self, label: str, argv: list[str], request: dict[str, Any]) -> dict[str, Any]:
        self.last_operation = label
        print(f"=== {label}", flush=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        started = time.monotonic()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = cli_main(argv, settings=self.settings)
        elapsed = time.monotonic() - started
        record: dict[str, Any] = {
            "operation": label, "status": "completed" if status == 0 else "failed",
            "exit_code": status, "elapsed_sec": round(elapsed, 3), "request": request,
        }
        if status == 0:
            try:
                result = json.loads(stdout.getvalue())
            except json.JSONDecodeError as exc:
                record["status"] = "failed"
                self._save_step(record)
                raise RuntimeError(f"{label} returned non-JSON operation output") from exc
            if label.startswith("search-"):
                result = safe_search_result(result)
            record["result"] = result
        else:
            # Provider messages and response bodies are deliberately omitted: they can
            # contain a signed download URL or other transient secret if a backend errs.
            record["error_code"] = safe_error_code(stderr.getvalue())
        self._save_step(record)
        print(f"--- {label}: {record['status']} in {elapsed:.2f}s", flush=True)
        if status != 0:
            raise RuntimeError(f"{label} failed; see the retained safe step status")
        return record["result"]

    def _save_step(self, record: dict[str, Any]) -> None:
        self.steps.append(record)
        self.write_json(f"{self.run_rel}/steps/{len(self.steps):02d}_{record['operation']}.json", record)

    def run(self, sketchfab_id: str | None) -> None:
        project = {"project_id": self.project_id, "title": "Asset sourcing smoke",
                   "output_profile": "low-light-bw"}
        self.cli("create-project", ["create-project", "--json", "--data", json.dumps(project)], project)
        self.store = ProjectStore(ProjectRoot(self.project_root))
        project_option = ["--project-id", self.project_id]

        searches = (
            ("search-polyhaven-models", {"provider": "polyhaven", "q": "wooden table",
                "asset_type": "model", "limit": 10, "page": 1}),
            ("search-sketchfab-models", {"provider": "sketchfab", "q": "simple chair",
                "asset_type": "model", "limit": 10, "page": 1}),
        )
        for label, request in searches:
            result = self.cli(label, ["search-assets", "--json", *project_option,
                "--data", json.dumps(request)], request)
            if result.get("provider") != request["provider"]:
                raise RuntimeError(f"{label} returned the wrong provider")

        for spec in PH_ASSETS:
            request = {"provider": "polyhaven", "expected_version": 0, **spec}
            label = f"acquire-{spec['asset_id']}"
            result = self.cli(label, ["acquire-asset", "--json", *project_option,
                "--data", json.dumps(request)], request)
            self.verify_asset(result["asset_id"], provider="polyhaven",
                              expected_license="CC0-1.0", expected_type=spec["asset_type"])

        if sketchfab_id is not None:
            request = {"provider": "sketchfab", "source_asset_id": sketchfab_id,
                       "asset_id": "sf_selected_model", "asset_type": "model",
                       "resolution": "1k", "format": "auto", "expected_version": 0}
            result = self.cli("acquire-sketchfab-model", ["acquire-asset", "--json",
                *project_option, "--data", json.dumps(request)], request)
            self.verify_asset(result["asset_id"], provider="sketchfab", expected_type="model")

        self.verify_browser()
        summary = {
            "schema_version": 1, "run_id": self.run_rel.rsplit("/", 1)[-1],
            "run_relative": self.run_rel, "project_relative": f"{self.project_rel}/{self.project_id}",
            "status": "completed", "operations": self.steps, "assets": self.asset_evidence,
            "artifacts": [f"{self.run_rel}/asset-browser.html",
                          f"{self.project_rel}/{self.project_id}/assets/registry"],
            "network_scope": "live low-resolution provider smoke; no credentials unless optional Sketchfab download enabled",
            "gpu_rendered": False,
        }
        self.write_json(f"{self.run_rel}/summary.json", summary)
        print(f"Smoke run complete: {self.run_rel}", flush=True)

    def verify_asset(self, asset_id: str, *, provider: str, expected_type: str,
                     expected_license: str | None = None) -> None:
        assert self.store is not None
        asset = self.store.load(Asset, asset_id)
        if asset.provider != provider or asset.generated or asset.type != expected_type:
            raise RuntimeError(f"registered asset {asset_id} has unexpected provider/generated metadata")
        if not asset.file_path or not asset.checksum:
            raise RuntimeError(f"registered asset {asset_id} lacks file path or checksum")
        project = self.store._root
        asset_path = project.resolve(asset.file_path)
        actual_size = asset_path.stat().st_size
        actual_hash = sha256(asset_path)
        if actual_hash != asset.checksum:
            raise RuntimeError(f"registered asset {asset_id} checksum does not match copied bytes")
        if expected_license is not None and asset.license != expected_license:
            raise RuntimeError(f"registered asset {asset_id} has unexpected licence")
        if not asset.source_url or not asset.source_url.startswith("https://") or not asset.attribution:
            raise RuntimeError(f"registered asset {asset_id} is missing safe source/attribution metadata")
        if provider == "polyhaven" and not asset.attribution_surface_required:
            raise RuntimeError(f"Poly Haven asset {asset_id} lost the application credit requirement")

        manifest_dir_rel = "assets/provider_cache/manifests"
        manifest_dir = project.resolve(manifest_dir_rel)
        matching: list[tuple[str, dict[str, Any]]] = []
        if manifest_dir.is_dir():
            for path in manifest_dir.glob("*.json"):
                rel = path.relative_to(project.path).as_posix()
                candidate = json.loads(project.read_text(rel))
                if (candidate.get("asset_id") == asset_id
                        and candidate.get("normalized_asset_path") == asset.file_path
                        and candidate.get("normalized_sha256") == actual_hash):
                    matching.append((rel, candidate))
        if len(matching) != 1:
            raise RuntimeError(f"asset {asset_id} has {len(matching)} matching immutable acquisition manifests")
        manifest_rel, manifest = matching[0]
        if (manifest.get("normalized_asset_path") != asset.file_path
                or manifest.get("normalized_sha256") != actual_hash
                or manifest.get("normalized_size") != actual_size):
            raise RuntimeError(f"asset {asset_id} manifest does not bind the registered bytes")
        if (manifest.get("provider") != provider or manifest.get("source_page") != asset.source_url
                or manifest.get("license") != asset.license or manifest.get("attribution") != asset.attribution):
            raise RuntimeError(f"asset {asset_id} manifest rights/source fields differ from registry")
        if provider == "polyhaven":
            if (asset.license != "CC0-1.0"
                    or manifest.get("license_url") != "https://creativecommons.org/publicdomain/zero/1.0/"
                    or not asset.source_url.startswith("https://polyhaven.com/a/")):
                raise RuntimeError(f"Poly Haven asset {asset_id} has unexpected rights/source URL")
        elif provider == "sketchfab":
            allowed_licenses = {
                "CC0-1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
                "CC-BY-4.0": "https://creativecommons.org/licenses/by/4.0/",
            }
            if (asset.license not in allowed_licenses
                    or manifest.get("license_url") != allowed_licenses.get(asset.license)
                    or asset.attribution_surface_required != (asset.license == "CC-BY-4.0")
                    or not asset.source_url.startswith("https://sketchfab.com/3d-models/")):
                raise RuntimeError(f"Sketchfab asset {asset_id} has unexpected rights/source URL")
        allowed_manifest_keys = {"asset_id", "provider", "id", "title", "creator", "source_page", "license",
            "license_url", "attribution", "resolution", "map", "format", "source_files",
            "conversion_version", "normalized_size", "normalized_asset_path", "normalized_sha256"}
        if not set(manifest).issubset(allowed_manifest_keys):
            raise RuntimeError(f"asset {asset_id} manifest contains unexpected fields")
        source_files = manifest.get("source_files")
        if not isinstance(source_files, list) or not source_files:
            raise RuntimeError(f"asset {asset_id} manifest has no provider source file hashes")
        for source in source_files:
            rel = source.get("relative_path")
            data = project.read_bytes(rel)
            if len(data) != source.get("size") or hashlib.sha256(data).hexdigest() != source.get("sha256"):
                raise RuntimeError(f"asset {asset_id} cached provider source hash/size mismatch")
            if source.get("md5") and hashlib.md5(data).hexdigest().lower() != source["md5"].lower():
                raise RuntimeError(f"asset {asset_id} cached provider source MD5 mismatch")
        self.asset_evidence.append({
            "asset_id": asset.asset_id, "provider": asset.provider, "type": asset.type,
            "source_url": asset.source_url, "license": asset.license,
            "attribution": asset.attribution,
            "attribution_surface_required": asset.attribution_surface_required,
            "file_path": asset.file_path, "sha256": actual_hash,
            "asset_artifact": f"{self.project_rel}/{self.project_id}/{asset.file_path}",
            "manifest_relative": f"{self.project_rel}/{self.project_id}/{manifest_rel}",
            "source_files_verified": [{"name": item["name"], "relative_path": item["relative_path"],
                                       "sha256": item["sha256"], "md5": item.get("md5")}
                                      for item in source_files],
        })

    def verify_browser(self) -> None:
        token = uuid.uuid4().hex
        app = create_app(self.settings, token)
        started = time.monotonic()
        with TestClient(app, base_url=f"http://127.0.0.1:{self.settings.server.port}") as client:
            response = client.get(f"/projects/{self.project_id}/assets/browser",
                                  params={"provider": "polyhaven", "asset_type": "model"})
        elapsed = time.monotonic() - started
        if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
            raise RuntimeError("asset browser did not return HTML successfully")
        body = response.text
        visible = html.unescape(body)
        if "Assets from Poly Haven" not in visible:
            raise RuntimeError("asset browser omitted the Poly Haven credit")
        for asset in self.asset_evidence:
            if asset["provider"] == "polyhaven" and (
                asset["asset_id"] not in visible or asset["license"] not in visible
                or asset["attribution"] not in visible
            ):
                raise RuntimeError(f"asset browser omitted saved credit for {asset['asset_id']}")
        self.root.write_text(f"{self.run_rel}/asset-browser.html", body)
        self._save_step({"operation": "asset-browser-html", "status": "completed",
            "elapsed_sec": round(elapsed, 3), "http_status": response.status_code,
            "artifact": f"{self.run_rel}/asset-browser.html",
            "visible_polyhaven_credit": True, "saved_assets_rendered": len(self.asset_evidence)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-sketchfab-download", action="store_true",
                        help="use the configured local Sketchfab key file for one explicit model download")
    parser.add_argument("--sketchfab-id", help="explicit 32-hex Sketchfab model UID; required with download flag")
    args = parser.parse_args()
    if args.with_sketchfab_download and not args.sketchfab_id:
        parser.error("--sketchfab-id is required with --with-sketchfab-download")
    if args.sketchfab_id and not args.with_sketchfab_download:
        parser.error("--sketchfab-id requires --with-sketchfab-download")
    run_id = time.strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]
    smoke = SmokeRun(run_id, args.with_sketchfab_download)
    print(f"Run directory: {smoke.run_rel}", flush=True)
    try:
        smoke.run(args.sketchfab_id)
    except Exception as exc:  # noqa: BLE001 - retain a safe failure record before exiting.
        # No exception text: provider diagnostics may otherwise echo sensitive URLs.
        smoke.write_json(f"{smoke.run_rel}/failure.json", {
            "status": "failed", "operation": smoke.last_operation,
            "error_type": type(exc).__name__, "message": "See safe per-operation records; raw diagnostics omitted."})
        raise SystemExit(
            f"Smoke run failed during {smoke.last_operation} ({type(exc).__name__}); "
            "see retained safe operation records."
        ) from None


if __name__ == "__main__":
    main()
