# Channel Connect

Use this file for single-instance IM onboarding only.

## Enter Conditions

Run `channel connect` only after:

- the OpenClaw instance exists
- status is usable
- the user asked for Weixin, Feishu, or another channel onboarding step

Typical commands:

```bash
agentengine openclaw channel connect --channel weixin
agentengine openclaw channel connect --channel feishu
```

If the current directory is not the OpenClaw working directory, pass the instance reference explicitly when supported by the command shape used in the current CLI.

## Exit Conditions

Treat the step as successful when one of the following happens:

- a QR code or onboarding screen is shown
- the remote gateway setup flow opens successfully
- the command reports the channel configuration was applied

Do not keep the session open after the requested entry point has been confirmed unless the user explicitly asks to continue through the interactive onboarding.
