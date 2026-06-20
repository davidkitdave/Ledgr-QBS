from pathlib import Path
import subprocess

import yaml


def test_deploy_workflow_parity_step_succeeds() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((repo_root / ".github/workflows/deploy.yml").read_text())
    steps = workflow["jobs"]["deploy-rc"]["steps"]
    parity_step = next(
        step
        for step in steps
        if step.get("name") == "Assert deploy flag parity with scripts/deploy-prod.sh"
    )

    result = subprocess.run(
        ["bash", "-e", "-c", parity_step["run"]],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
