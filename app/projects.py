from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Sequence

from .monitor import Config, MonitorError, SheetSource, save_state


def registry_path(config: Config) -> Path:
    return config.state_path.parent / "projects.json"


def load_projects(config: Config) -> List[Dict[str, object]]:
    path = registry_path(config)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as error:
        raise MonitorError("Projects registry is not valid JSON") from error
    if isinstance(value, dict):
        value = value.get("projects", [])
    if not isinstance(value, list):
        raise MonitorError("Projects registry must contain a list")
    return [entry for entry in value if isinstance(entry, dict)]


def save_projects(config: Config, projects: Sequence[Dict[str, object]]) -> None:
    save_state(registry_path(config), {"projects": list(projects)})


def project_id(name: str, client_spreadsheet_id: str) -> str:
    payload = (name.strip() + "|" + client_spreadsheet_id.strip()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def config_for_project(base: Config, project: Dict[str, object]) -> Config:
    try:
        name = str(project["name"]).strip()
        client_sheet = str(project["client_spreadsheet_id"]).strip()
        autoload_sheet = str(project["autoload_spreadsheet_id"]).strip()
        client_id = str(project["avito_client_id"]).strip()
        client_secret = str(project["avito_client_secret"]).strip()
        delay_hours = int(project.get("delay_hours", 5))
    except (KeyError, TypeError, ValueError) as error:
        raise MonitorError("Project registry contains an invalid project") from error
    if not all((name, client_sheet, autoload_sheet, client_id, client_secret)) or delay_hours <= 0:
        raise MonitorError("Project registry contains an incomplete project")
    identifier = str(project.get("id") or project_id(name, client_sheet))
    return replace(
        base,
        project_name=name,
        client_sources=(SheetSource("Все листы (авито)", client_sheet, ""),),
        client_spreadsheet_ids=(client_sheet,),
        autoload_sources=(SheetSource("Все листы Avito", autoload_sheet, ""),),
        autoload_spreadsheet_ids=(autoload_sheet,),
        avito_client_id=client_id,
        avito_client_secret=client_secret,
        state_path=base.state_path.parent / ("state-%s.json" % identifier),
        mismatch_grace=timedelta(hours=delay_hours),
    )


def all_project_configs(base: Config) -> List[Config]:
    configs = [base]
    for project in load_projects(base):
        configs.append(config_for_project(base, project))
    return configs
