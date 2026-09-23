"""Exercise the installed SDK without local accounts or provider access."""

from tempfile import TemporaryDirectory

from agentbridge import Bridge


def main():
    with TemporaryDirectory(prefix="agentbridge-example-") as root:
        with Bridge(root) as bridge:
            capabilities = bridge.capabilities()
            models = bridge.models()
            assert capabilities["execution_engine"] == "codex"
            assert capabilities["route"] == "local_cliproxyapi"
            assert models["items"] == []
            print("Installed AgentBridge SDK: Codex via local proxy")


if __name__ == "__main__":
    main()
