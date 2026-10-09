# Gateway Deployment Implementation Plan

**Goal:** Complete the authorized deployment steps 1–11 and restore gateway startup.

**Architecture:** Existing API and policy gateway run as lfy behind a loopback TLS proxy. Remote users access the proxy through SSH forwarding. Company provider setup and actual evaluations remain outside this deployment stage.

**Tech Stack:** Python 3.11, systemd, Nginx, websockets 15.

## Bounded startup repair

- [x] Reproduce startup failure from systemd journal: configured_reference_path is not exported by roboicl.config.
- [x] In roboicl/policy_gateway.py import reference_path as configured_reference_path; preserve existing call sites.
- [x] Run `/home/lfy/.conda/envs/roboicl-policy/bin/python -m roboicl.policy_gateway --help` as lfy from the repository; expect exit 0.
- [x] Restart roboicl-policy-gateway and verify it remains active.
- [x] Run `/home/lfy/.conda/envs/roboicl-policy/bin/python /tmp/roboicl-verify-services.py` as lfy; expect HTTPS 401 without credentials, health 200, invalid session 422, session creation 201 and unauthenticated WSS 401. Do not submit a run.
- [x] Prepare private client files and confirm server SSH forwarding configuration. User-side VPN connectivity must be checked from the user's computer.

Existing user changes are preserved; no commits or third-party edits are required.

Verified SSH forwarding locally using hjx and a real SSH connection to 127.0.0.1. External client/VPN connectivity remains unverified.
