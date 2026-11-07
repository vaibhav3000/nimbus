import os
import json
import platform
import psutil
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

GIT_COMMIT = "a87763fa837e38648a1c83ed5ae3b15fc28f5456"

# Color palette: C1 blue, C2 orange, C3 grey
COLOR_C1 = "#1f77b4"
COLOR_C2 = "#ff7f0e"
COLOR_C3 = "#7f7f7f"
COLOR_PASS_BAR = "#d62728"

def get_env_dict():
    return {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count_logical": os.cpu_count(),
        "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
        "python_version": platform.python_version(),
        "aws_region": "ap-south-1",
        "aws_account_id": "930124786284",
        "bedrock_model_id": "apac.amazon.nova-lite-v1:0",
        "git_commit": GIT_COMMIT,
    }

def write_env_json(output_dir: str):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "env.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(get_env_dict(), f, indent=2)
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
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "README.md")
    content = f"{claim.strip()}\n{pass_bar.strip()}\n{command.strip()}\n{git_commit.strip()}\n{result_line.strip()}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
