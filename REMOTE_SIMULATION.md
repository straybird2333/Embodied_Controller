# Remote simulation API

The API queues RoboDojo evaluations on the simulator host and launches the
simulator as an isolated child process. The policy WSS gateway pairs that
simulator connection with a client-initiated connection from the external
policy process, then relays the existing RoboDojo RPC stream unchanged. The
LLM provider and its credentials stay on the external policy machine; the
simulator host does not need an LLM provider key.

The deployment recorded in `handover.md` has a working Isaac Sim 5.1.0 capture
path, ffmpeg 4.4.2, and three 640×480 cameras. See the handover for exact paths
and the scope of the completed simulator validation.

## Request flow

Create a short lived policy session. Its response contains a public WSS URL
and a single-use, session-scoped bearer token for the external policy client.
Start the client connector before submitting a run; it holds an outbound WSS
connection while waiting for the simulator. Then submit a run with the
returned `policy_session` ID and the same task settings. The API gives the
simulator a separate internal ticket; it never returns that ticket to the
caller. The gateway checks both tickets for signature, expiry, scope, and
replay. The API assigns each accepted run to an available GPU from the
configured pool; status responses include the assigned `gpu` once a worker
starts. The included deployment selects GPUs 0–7 and allows eight concurrent
runs.

```sh
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"task":"deposit_coin","shots":0,"seed":0,"layout":0,"profile":"roboicl-0shot-b25-v1"}' \
  "$API/v1/policy-sessions"
```

Keep `policy_session`, `external_policy_ws_url`, and
`external_policy_token` in private memory or a mode-0600 file. Do not log or
share the external token. The helper in `roboicl.external_policy_client`
accepts a policy object implementing the XPolicy model methods (normally
`reset`, `update_obs`, and `get_action`). `update_obs` receives decoded
observation images and proprioception; `get_action` returns the corresponding
RoboDojo action chunk. The helper handles the pinned MessagePack/RPC protocol
and authentication. Implement the model callback on the external machine so
it calls your chosen LLM service there.

For example, the external client can load the session response from a private
file and run a policy adapter:

```python
import asyncio
import json
import sys
from pathlib import Path

from roboicl.external_policy_client import run_external_policy_session
from my_external_policy import MyPolicy

session_path = Path(sys.argv[1])
session = json.loads(session_path.read_text())
session_path.unlink()  # the short-lived bearer token is now only in memory
asyncio.run(run_external_policy_session(
    session["external_policy_ws_url"],
    session["external_policy_token"],
    MyPolicy(),
))
```

`MyPolicy` stores observations in `update_obs` and calls your external model
service from `get_action`. Its result must follow RoboDojo's registered action
shape and limits; the WSS adapter transports RPC and does not reinterpret
arbitrary model output.

Submit the run using exactly the same task settings and the returned session
ID:

```sh
curl --fail-with-body --silent --show-error \
  -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"task":"deposit_coin","shots":0,"seed":0,"layout":0,"profile":"roboicl-0shot-b25-v1","policy_session":"<session-id>"}' \
  "$API/v1/runs"
```

Poll the returned `status_url`, request cancellation with `cancel_url`, and
fetch the evaluator result from `/v1/runs/{run_id}/result`. The API does not
provide arbitrary file or log downloads. See
`docs/remote-simulation-openapi.yaml` for the API contract and
`docs/remote-simulation-ops.md` for deployment details.

The policy profile must be a checked-in profile under `configs/protocols/` or
an administrator-installed JSON file directly under the simulator host's
`ROBOICL_PROFILE_DIR`. It defines the allowed task/action profile; it does not
select the external client's LLM provider. Clients cannot submit arbitrary
configuration files or commands.

The API and policy gateway default to loopback listeners. The reverse proxy
routes `/session/{id}` for the simulator and `/external/{id}` for the external
client. The external client initiates its WSS connection, so the simulator
host needs outbound connectivity only. Do not expose simulator ports or an
unauthenticated XPolicyLab policy server.
