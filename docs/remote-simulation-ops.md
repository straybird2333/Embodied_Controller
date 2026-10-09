# Remote simulation service operations

The control API launches `roboicl.run` with the configured Isaac Sim
interpreter. The policy WSS gateway pairs the simulator with an external
policy client and relays RoboDojo RPC messages. Keep both services on loopback
and put TLS termination and public routing in a reverse proxy. The simulator
host does not need the LLM provider key.

## Secrets and paths

Copy `setup/remote-api.env.example` to `/etc/roboicl/remote-api.env` on the
simulator/API host. Set the interpreter and data paths to the values from
`handover.md`, then create the access key and private ticket-signing key:

```sh
sudo install -d -m 0750 -o root -g lfy /etc/roboicl
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/roboicl/access-keys'
sudo sh -c 'umask 077; openssl rand -hex 32 > /etc/roboicl/policy-ticket.key'
sudo chown lfy:lfy /etc/roboicl/access-keys /etc/roboicl/policy-ticket.key
sudo chmod 0600 /etc/roboicl/access-keys /etc/roboicl/policy-ticket.key
sudo chown root:lfy /etc/roboicl/remote-api.env /etc/roboicl/policy-gateway.env
sudo chmod 0640 /etc/roboicl/remote-api.env /etc/roboicl/policy-gateway.env
```

The API and gateway share `policy-ticket.key` with mode 0600. Access keys,
ticket-signing keys, external policy-session tokens, and provider keys serve
different purposes. The API accepts multiple access keys on separate lines
during key rotation. Provider keys stay on the external policy client. Do not
put secrets in command arguments, URLs, or checked-in files.

The external policy client needs the RoboICL protocol adapter and an object
implementing the XPolicy model methods. It may call any provider available to
that client. Its provider credentials remain on that machine. The simulator
assets and Isaac Sim environment stay on the simulator host.

The API validates profiles against `configs/protocols` and the optional
simulator-host `ROBOICL_PROFILE_DIR`. The selected profile defines the allowed
task/action scope; it does not select or authenticate an LLM provider.

## Install services

Install `setup/roboicl-api.service.example` as
`/etc/systemd/system/roboicl-api.service`. Install
`setup/policy-gateway.env.example` as `/etc/roboicl/policy-gateway.env` and
`setup/roboicl-policy-gateway.service.example` as
`/etc/systemd/system/roboicl-policy-gateway.service` on the simulator host.
Set the service working directory and ticket-secret path for the deployment.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now roboicl-policy-gateway
sudo systemctl enable --now roboicl-api
sudo systemctl status roboicl-policy-gateway roboicl-api
```

The API binds to `127.0.0.1:8080`; the policy gateway binds to
`127.0.0.1:8765`. Set `ROBOICL_POLICY_WSS_URL` to the simulator-side WSS base
ending in `/session`, for example `wss://127.0.0.1:8443/session`. The reverse
proxy routes `/session/{session_id}` and `/external/{session_id}` to the
gateway and preserves `Authorization` and WebSocket upgrade headers. For the
external URL, the API uses the `X-Forwarded-Host` set by the trusted reverse
proxy. `ROBOICL_API_GPUS` configures the GPU pool and
`ROBOICL_API_MAX_PARALLEL` controls simultaneous runs; the checked-in example
uses GPUs 0–7 with eight concurrent runs. Each run reports its assigned `gpu`
in the status response. The gateway's `ROBOICL_POLICY_GATEWAY_MAX_WORKERS`
must be at least the API parallel limit.
The example API permits 40 session/run creation requests per caller IP per
minute, enough to submit eight sessions and eight runs together; tune this
with `ROBOICL_API_CREATE_RATE_PER_MIN` if needed.

The gateway does not start a model process or read provider credentials.
Do not expose the raw XPolicyLab worker port, simulator ports, API HTTP port,
or gateway listener publicly.

## HTTPS and WSS reverse proxy

Example Nginx routing for one public hostname (define the `limit_req_zone`
inside Nginx's `http` block):

```nginx
limit_req_zone $binary_remote_addr zone=roboicl_api:10m rate=10r/m;

server {
    listen 443 ssl;
    server_name roboicl.example.net;
    ssl_certificate /etc/letsencrypt/live/roboicl.example.net/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/roboicl.example.net/privkey.pem;
    client_max_body_size 16k;

    location /v1/ {
        limit_req zone=roboicl_api burst=10 nodelay;
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 30s;
    }

    location /session/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 8000s;
    }

    location /external/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 8000s;
    }

    location / { return 404; }
}
```

Set the API's internal simulator WSS base to
`wss://127.0.0.1:8443/session`. The WSS proxy terminates TLS and forwards both
upgraded routes to the loopback gateway. Firewall public access to HTTPS/WSS
only. Use a controlled outbound tunnel if the simulator host is behind CGNAT.
The external policy client connects outbound to `/external/{id}`.

## API workflow

Create a one-time session, start a client connector using its
`external_policy_ws_url` and `external_policy_token`, then submit a run using
the returned session ID and identical task settings. The token is a secret;
the connector should keep it in memory and send it only as the WSS bearer
header. See `REMOTE_SIMULATION.md` for the SDK interface.

```sh
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"task":"deposit_coin","shots":0,"seed":0,"layout":0,"profile":"roboicl-0shot-b25-v1"}' \
  https://roboicl.example.net/v1/policy-sessions
```

Use its returned `policy_session` value in `POST /v1/runs`; the ID is not a
secret. The server separately issues the simulator's signed WSS ticket. Poll the returned
`status_url`, cancel through `cancel_url`, and read the whitelisted result from
`/v1/runs/{run_id}/result`. The API has no file or log download route. See
`docs/remote-simulation-openapi.yaml` for the full contract.

The API stores its SQLite registry and bounded private logs in the configured
state directory. Results are written by the existing runner and removed after
the configured retention period. One API process owns each state directory;
the configured worker pool runs at most one evaluation per assigned GPU.


## Live deployment recorded on 2026-10-09 (Asia/Shanghai)

- API, policy gateway, local TLS proxy, and `roboicl-cloudflared.service` are
  active and enabled. API and gateway bind to loopback; the tunnel establishes
  an outbound connection to Cloudflare because upstream ingress is blocked.
- Temporary public API URL:
  `https://drain-organizer-stewart-publisher.trycloudflare.com`. Quick Tunnel
  URLs can change after restart and have no uptime guarantee.
- Nginx at `/etc/roboicl/nginx.conf` routes `/v1/`, simulator `/session/`, and
  external policy `/external/` through loopback TLS. No simulator port is
  directly exposed.
- Public authenticated health check returns ready, GPUs 0–7, and parallel limit
  8. An external WSS session token was issued and its public authenticated
  handshake succeeded. This handshake did not launch a simulator or call a
  model.
- The policy gateway now relays to a client-initiated external policy connector;
  it does not load a provider key or select Qwen. The provider credential stays
  on the external client.
- A prior user-submitted run failed before this relay mode was installed because
  the previous gateway configuration referenced a missing provider-key file.
  It has no completed evaluator result and is not a validation of external
  policy execution.

The current health endpoint checks TLS reachability at the proxy, not a full
gateway application handshake or model credentials. ready=true does not certify
that a model evaluation can run; use the WSS check and a later real evaluation.

Client files are prepared in `/home/hjx/roboicl-client/` (directory mode 0700):
`server.crt`, `health.py`, and `access-key` (mode 0600). The directory contains a
credential and must be transferred privately, never committed to the repository.
The temporary local test tunnel was closed after verification.

On the user's computer, after connecting the campus VPN, copy the directory
using the normal SSH address/account, then open a tunnel:

```bash
scp -r hjx@YOUR_SSH_HOST:/home/hjx/roboicl-client ./roboicl-client
chmod 700 ./roboicl-client
chmod 600 ./roboicl-client/access-key
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 127.0.0.1:18443:127.0.0.1:8443 hjx@YOUR_SSH_HOST
```

Keep the tunnel open and in a second terminal run:

```bash
python3 ./roboicl-client/health.py
```

Installation used a temporary apt source list with the NJU HTTPS Ubuntu mirror
because the configured Aliyun HTTP path returned an invalid signed response.
The global apt source files were not modified. No CUDA or Docker upgrade was run.
