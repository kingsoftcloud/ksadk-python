"""KsADK Skill Runtime image entrypoint.

Sandbox runtime images should copy this file to:

    /home/ksadk/agent.py

The implementation lives in the installed ``ksadk`` package so the image can
receive normal SDK upgrades without changing the entrypoint contract.
"""

from __future__ import annotations

from ksadk.skills.runtime.agent import main


if __name__ == "__main__":
    raise SystemExit(main())
