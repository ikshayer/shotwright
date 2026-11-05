from pathlib import Path

from orchestrator.adapters.blender.runner import build_command


def test_build_command_is_exact_argv_without_shell() -> None:
    exe = Path(r"D:\Blender\blender.exe")
    script = Path(r"C:\project\renders\worker.py")
    plan = Path(r"C:\project\renders\plan.json")
    assert build_command(exe, script, plan) == [
        str(exe), "--background", "--factory-startup", "--disable-autoexec",
        "--python-exit-code", "1", "--python", str(script), "--", str(plan),
    ]
