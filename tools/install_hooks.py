"""Enable AgentBridge's tracked Git hooks for the current checkout."""
from pathlib import Path
import subprocess


def main():
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "-C", str(root), "config", "core.hooksPath", ".githooks"], check=True)
    print("Enabled AgentBridge hooks at .githooks.")


if __name__ == "__main__":
    main()
