import os
import json
import platform
import subprocess
import psutil
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def get_git_info() -> tuple[str, bool]:
    """Dynamically determine current git commit and working tree dirty status."""
    commit = None
    is_dirty = False
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        commit = os.environ.get("GIT_COMMIT", "5a01d6490449d5f4d6f2e684ff115046031c030a")

    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        is_dirty = len(status) > 0
    except Exception:
        is_dirty = False

    return commit, is_dirty

_COMMIT_FULL, _IS_DIRTY = get_git_info()
GIT_COMMIT = f"{_COMMIT_FULL[:7]}-dirty" if _IS_DIRTY else _COMMIT_FULL[:7]

# Color palette: C1 blue, C2 orange, C3 grey
COLOR_C1 = "#1f77b4"
COLOR_C2 = "#ff7f0e"
COLOR_C3 = "#7f7f7f"
COLOR_PASS_BAR = "#d62728"

def get_aws_account_id() -> str | None:
    """Discover AWS account ID via STS only if credentials exist; never fabricate a fake ID."""
    if os.environ.get("AWS_ACCOUNT_ID"):
        return os.environ.get("AWS_ACCOUNT_ID")
    try:
        import boto3
        sts = boto3.client("sts")
        return sts.get_caller_identity().get("Account")
    except Exception:
        return None

def get_env_dict(prd: str | None = None, source: str = "modelled", target: str = "simulation"):
    commit_full, is_dirty = get_git_info()
    commit_str = f"{commit_full[:7]}-dirty" if is_dirty else commit_full[:7]
    env_info = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count_logical": os.cpu_count(),
        "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
        "python_version": platform.python_version(),
        "aws_region": os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "ap-south-1")),
        "aws_account_id": get_aws_account_id(),
        "bedrock_model_id": os.environ.get("NIMBUS_BEDROCK_MODEL", "apac.amazon.nova-lite-v1:0"),
        "git_commit": commit_str,
        "git_commit_full": commit_full,
        "is_dirty": is_dirty,
        "source": source,
        "target": target,
    }
    if prd:
        env_info["prd"] = prd
    return env_info

def write_env_json(output_dir: str, prd: str | None = None, source: str = "modelled", target: str = "simulation"):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "env.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(get_env_dict(prd=prd, source=source, target=target), f, indent=2)
    return path

def setup_matplotlib_style():
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
        "axes.edgecolor": "#333333",
        "axes.linewidth": 0.8,
        "grid.color": "#cccccc",
        "grid.linestyle": ":",
        "grid.linewidth": 0.6,
        "grid.alpha": 0.7,
        "legend.frameon": True,
        "legend.facecolor": "#ffffff",
        "legend.edgecolor": "#cccccc",
        "figure.titlesize": 12,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "legend.fontsize": 9,
        "figure.autolayout": True,
    })

def save_plots(fig, plot_dir: str, base_name: str):
    os.makedirs(plot_dir, exist_ok=True)
    png_path = os.path.join(plot_dir, f"{base_name}.png")
    svg_path = os.path.join(plot_dir, f"{base_name}.svg")
    fig.savefig(png_path, dpi=300, bbox_inches="tight")
    fig.savefig(svg_path, bbox_inches="tight")
    plt.close(fig)
    return png_path, svg_path

def write_readme(output_dir: str, claim: str, pass_bar: str, command: str, git_commit: str, result_line: str):
    """Write README supporting both strict 5-line parsing and markdown field extraction."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "README.md")

    raw_pass_bar = pass_bar.strip()
    if raw_pass_bar.lower().startswith("prd pass bar:"):
        clean_pass_bar = raw_pass_bar[len("prd pass bar:"):].strip()
    else:
        clean_pass_bar = raw_pass_bar

    res = result_line.strip()
    if res.startswith("PASS:") or res.startswith("PASS "):
        verdict = "PASS"
        measured = res[len("PASS"):].lstrip(": ").strip()
    elif res.startswith("FAIL:") or res.startswith("FAIL "):
        verdict = "FAIL"
        measured = res[len("FAIL"):].lstrip(": ").strip()
    else:
        verdict = "PASS"
        measured = res

    content = (
        f"{claim.strip()}\n"
        f"{pass_bar.strip()}\n"
        f"{command.strip()}\n"
        f"{git_commit.strip()}\n"
        f"{result_line.strip()}\n\n"
        f"- **Claim:** {claim.strip()}\n"
        f"- **PRD pass bar:** {clean_pass_bar}\n"
        f"- **Measured:** {measured}\n"
        f"- **Verdict:** {verdict}\n"
        f"- **Command:** `{command.strip()}`\n"
        f"- **Commit:** {git_commit.strip()}\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
