"""Rebuild every video and image in docs/assets from real roadtest runs.

    .venv/bin/python media/build.py            # everything
    .venv/bin/python media/build.py demo       # only the README demo

It records the CLI's actual output (media/capture.py), then renders the HTML scenes frame by frame
(media/render.mjs, which needs Node, Google Chrome and ffmpeg). The launch video's soundtrack is
synthesised from the scene's cues (media/sound.py). Set OPENAI_API_KEY to include the judge's
verdicts in the recordings; without it the rubric lines show as skipped.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "media"
ASSETS = ROOT / "docs" / "assets"
PYTHON = sys.executable
sys.path.insert(0, str(ROOT / "src"))


def capture(name: str, shown: str, command: list[str], cwd: Path) -> None:
    environment = {**os.environ, "COLUMNS": "92"}
    subprocess.run([PYTHON, str(MEDIA / "capture.py"), name, shown, "--", *command], cwd=cwd, env=environment, check=True)


def render(scene: str, output: Path, *flags: str) -> None:
    subprocess.run(["node", str(MEDIA / "render.mjs"), str(MEDIA / "scenes" / scene), str(output), *flags], check=True)


def gif(video: Path, output: Path, width: int = 1100) -> None:
    palette = (
        f"fps=12,scale={width}:-1:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=48:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
    )
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", palette, "-loop", "0", str(output)], check=True)


def readme_demo(work: Path) -> None:
    capture("demo", "roadtest demo", [PYTHON, "-m", "roadtest", "demo", "--out", str(work / "demo-results")], work)
    render("demo.html", MEDIA / "out" / "demo.mp4", "--width", "1640", "--height", "1300")
    gif(MEDIA / "out" / "demo.mp4", ASSETS / "demo.gif")


def launch(work: Path) -> None:
    from roadtest.demo import agent as demo_agent

    project = work / "my-agent"
    project.mkdir()
    capture("init", "roadtest init", [PYTHON, "-m", "roadtest", "init"], project)

    server, url = demo_agent.start()
    try:
        config = project / "evals" / "roadtest.yaml"
        config.write_text(config.read_text().replace("http://localhost:8000/chat", url), encoding="utf-8")
        scenarios = project / "evals" / "scenarios"
        for old in scenarios.glob("*.yaml"):
            old.unlink()
        shutil.copy(Path(demo_agent.__file__).parent / "scenarios" / "support.yaml", scenarios / "support.yaml")
        judged = bool(os.environ.get("OPENAI_API_KEY"))
        command = [PYTHON, "-m", "roadtest", "run"] + ([] if judged else ["--no-judge"])
        capture("run", "roadtest run" + ("" if judged else " --no-judge"), command, project)
    finally:
        server.shutdown()

    run_file = sorted((project / "evals" / "results").glob("*/run.json"))[-1]
    report = MEDIA / "out" / "report.html"
    subprocess.run([PYTHON, str(MEDIA / "report_sample.py"), str(run_file), str(report)], check=True)
    shot = ["--dark", "--hash", "refund-outside-window", "--top", "--still", "0"]
    subprocess.run(["node", str(MEDIA / "render.mjs"), str(report), str(ASSETS / "report.png"), "--width", "1440", "--height", "1400", *shot], check=True)
    subprocess.run(["node", str(MEDIA / "render.mjs"), str(report), str(MEDIA / "captures" / "report.png"), "--width", "1180", "--height", "900", "--scale", "2", "--full", *shot], check=True)

    silent, cues = MEDIA / "out" / "launch.mp4", MEDIA / "out" / "launch-cues.json"
    render("launch.html", silent, "--width", "1920", "--height", "1080")
    render("launch.html", cues, "--cues")
    subprocess.run([PYTHON, str(MEDIA / "sound.py"), str(cues), str(silent), str(ASSETS / "launch.mp4")], check=True)
    render("launch.html", ASSETS / "launch-poster.png", "--width", "1920", "--height", "1080", "--still", "6")


def main() -> None:
    wanted = set(sys.argv[1:]) or {"demo", "launch"}
    (MEDIA / "out").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="roadtest-media-") as folder:
        work = Path(folder)
        if "demo" in wanted:
            readme_demo(work)
        if "launch" in wanted:
            launch(work)


if __name__ == "__main__":
    main()
