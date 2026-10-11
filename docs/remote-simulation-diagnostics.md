# Remote simulation startup and transfer diagnostics

2026-10-10 update: the minute-scale first-step stall was traced to remote
material/resource resolution. Original MDL modules and textures now have
verified local URL aliases. New API probes reached their first observation in
54.21–73.34 seconds. See [the fix and measurements](first-frame-material-cache-fix.md).
Earlier timings below are historical measurements before this fix.

The diagnostic endpoints expose the stages of API-managed simulation runs and
provide a no-LLM first-frame probe. They use the existing queue, GPU allocator,
runner, and cancellation path. A diagnostic run does not create a policy
session, connect to the external policy WebSocket, call an LLM, or upload files.

## Inspect capabilities

```sh
curl -fsS -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/diagnostics/capabilities" | jq
```

The response declares the source commit and whether the checkout has local
changes. GPU utilization and memory are whole-card samples; process CPU, RSS,
and IO counters are sampled for the runner and its descendants. GPU use cannot
be attributed to one run when other processes share that card.

## Start the no-LLM first-observation probe

```sh
DIAG_RUN=$(curl -fsS -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"task":"deposit_coin","shots":0,"seed":0,"layout":0,"profile":"roboicl-0shot-b25-v1","mode":"first_observation","max_steps":0,"timeout_seconds":1800}' \
  "$API/v1/diagnostic-runs")
RUN_ID=$(printf '%s' "$DIAG_RUN" | jq -er '.run_id')
printf '%s\n' "$DIAG_RUN" | jq
```

Poll while it runs. `after_seq` is an exclusive cursor, so use the returned
`next_seq` for the next page. Resource samples are events in the same sequence.

```sh
curl -fsS -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/runs/$RUN_ID/diagnostics?after_seq=0&limit=200" | jq
curl -fsS -H "Authorization: Bearer $ROBOICL_CLIENT_ACCESS_KEY" \
  "$API/v1/runs/$RUN_ID/logs?source=client&tail_lines=200" | jq
```

The `first_observation` mode follows normal runner preflight, Isaac startup,
environment creation, layout stability, reset/warmup, and valid three-camera
capture, then exits. It reports `diagnostic_success=true` and
`task_success=null`; it does not score task performance.

For a short control-path timing sample, request `mode=fixed_steps` with
`max_steps` from 1 through 20 (default 5). It holds the observed end-effector
poses and gripper positions, runs the normal action and observation path, and
keeps the IK continuity guard enabled. If a hold action is rejected, the run
records the rejection and stops without retrying it as an executed step.

## Event and timing rules

Events include increasing `seq`, UTC timestamps, host-boot-scoped monotonic
timestamps, source, span ID, attempt, step, request ID, and attributes. Duration
spans are measured within one host monotonic clock. Nested phases overlap; do
not add parent and child durations together. Resource snapshots are emitted
about every two seconds, capped at 20,000 events per run, and retained for the
same seven-day run retention period.

`first_observation_ready_seconds` measures from API acceptance using the same
host monotonic clock. `observation_rpc` is the full matching WebSocket RPC
round trip; it includes client processing and is not one-way network latency or
pure LLM inference. `observation_encode` reports MessagePack serialization
time and full message size. The current raw RGB transport is reported as used;
JPEG comparison is not implemented by these P0 endpoints.

The logs endpoint only exposes fixed `runner`, `client`, and `camera` sources,
with at most 1,000 lines and 256 KiB. Missing source files return a not-found
error. Known bearer/API token patterns are redacted.

## Current stage coverage

Instrumented stages include queue wait/GPU assignment, runner resource and asset
preflight, submodule checks, runner/simulator process launch, simulator Python
entry and Isaac imports, AppLauncher, environment creation/reset, layout
stability, reset warmup counters, camera readiness, observation serialization
and RPC round trip, policy action RPC wait, action execution/rejection,
process/resource sampling, and cleanup. Gaps in a trace remain unassigned and
are listed as unknown; instrumentation does not fill them with estimates.

## Observed first-observation runs (2026-10-10)

Both probes used `deposit_coin`, shots 0, seed 0, layout 0, profile
`roboicl-0shot-b25-v1`, with no LLM, external policy session, action step, or
file upload. Both ended with `diagnostic_success=true`, `task_success=null`,
`completed_steps=0`, and cleanup complete.

| Run ID | GPU | First observation from API acceptance | Main stages |
| --- | ---: | ---: | --- |
| `c16b0224b0ff485ab3b0595a781a8383` | 0 | 827.07 s (13:47) | `app_launch` 13.94 s; `env_create` 20.13 s; `env_reset` 791.62 s; `layout_stability` 1.93 s; `warmup` 12.90 s; first observation capture 0.41 s |
| `113506d1148f405c828f2e70e9960138` | 1 | 763.63 s (12:44) | `app_launch` 13.08 s; `env_create` 20.51 s; `scene_reload_envs` 710.30 s; `env_reset` 728.64 s; `layout_stability` 2.13 s; `warmup` 12.54 s; first observation capture 0.38 s |

The second run had reset substage instrumentation enabled. `scene_reload_envs`
accounted for 710.30 seconds, about 93% of its acceptance-to-first-observation
time. It recreated the saved scene objects. Robot reset took 0.002 s, camera
reset 0.021 s, capture reset 0.145 s, and reward/observation manager resets
under 0.004 s each. The 300-step layout stability pass took 2.13 s; the 200
warmup steps, 90 render calls, and 40 camera captures took 12.54 s. A separate
600-step progress counter reached 600 in about 716 s, so physics stepping is
not the main source of the long wait.

During `scene_reload_envs`, 334 resource samples showed whole-card GPU
utilization at 0% (GPU memory stayed near 6.9 GiB). The simulator process used
about 87 CPU seconds over the 710-second span, RSS grew from 6.61 to 6.65 GiB,
and its Linux block-device `read_bytes` counter stayed at zero. This does not
measure network reads. Near the end of the stage, the client log reported
invalid MDL modules for Omniverse S3 URLs including `Plastic_ABS.mdl`,
`Aluminum_Anodized.mdl`, and `Aluminum_Cast.mdl`; the same errors appeared in
both runs. This is evidence for a slow or failing external USD/MDL asset
resolution path, but the current metrics do not prove that those requests
account for the full 710 seconds. No production optimization has been applied.

The deployed API reported source commit
`df7b195dca7b71756df7acc95b7dfc0f131803b6` with a dirty working tree. The
observations above are from those live API runs. Capabilities currently report
the configured asset root on a local `ext4` filesystem and complete-tree
manifest/hash preflight. The public API used for these probes was
`https://drain-organizer-stewart-publisher.trycloudflare.com`; use the current
`API` value if the public tunnel address changes.

## Observed fixed-step run (2026-10-10)

Run `d9d13ffd5c1947e995984c999866d360` used `fixed_steps=5` on GPU 0. It
completed all five hold-pose steps with no IK rejection and cleanup complete.
It took 1,012.87 seconds to the first observation because `scene_reload_envs`
took 959.31 seconds in this run. After reset, each of the five valid
three-camera intermediate observations took about 0.10–0.11 seconds in the
simulator (`observation_capture` spans: 0.102, 0.102, 0.102, 0.107, and
0.107 seconds). The five hold-pose `take_action` calls totaled 0.206 seconds;
the first call was 0.205 seconds and the remaining four were under 0.001
seconds each. These zero-delta actions measure command-path overhead, not the
cost of a real moving action.

This mode directly uses the local diagnostic stub. It measures simulator-side
capture and hold-command execution, but does not send observations through the
policy WebSocket, MessagePack transport, external client, or LLM. Thus the
simulator-side middle-frame capture is confirmed fast; end-to-end middle-turn
latency still needs separate WSS/RPC and policy measurements. JPEG/WSS
comparison has not been run.

## Repeated live first-observation measurements (2026-10-10)

Two more `first_observation` runs were submitted through the API after adding
scene component/object timing. They used the same task, seed, and layout; no
LLM or actions were involved. Both succeeded and released their GPU slots.

| Run ID | GPU | API acceptance to first observation | `scene_reload_envs` | App launch | Env create | Warmup |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `a3055a8ae5ae44289972bfa2edc8fb1d` | 1 | 906.21 s (15:06) | 852.39 s (14:12) | 12.88 s | 19.66 s | 13.65 s |
| `e5712e83f88c4603852f0545a13f6bfb` | 2 | 1,072.12 s (17:52) | 1,018.65 s (16:59) | 13.28 s | 20.76 s | 12.67 s |

The second run narrows the long interval. `scene_reload_env_scene` took 0.89 s;
all four object `initialize()` calls totaled under 1 ms; object creation took
0.11 s; and stale-object relocation took 0.01 s. The event timeline then has a
1,017.75 s gap between the last object initialization and the next stale-object
relocation. In `SceneManager.reload_envs`, the only operation between those
points is `self.sim.sim_step()`. This strongly localizes the wait to that first
physics step after scene reload. The adapter now emits `slow_physics_step`
after any diagnostic physics step taking at least 100 ms, and records slow
`simulation_context_step`, `scene_write_data_to_sim`, and `scene_update`
subcalls separately. This identifies which part of the physics step consumed
the time without logging every normal warmup step.

During that 1,018 s gap, 478 resource samples showed whole-card GPU
utilization at 0%, GPU memory fixed near 6.9 GiB, simulator CPU time increasing
about 115 s, RSS fixed near 6.64 GiB, and Linux block-device reads at zero.
These counters do not include network reads and do not identify what the
physics backend is waiting on. The client log reported invalid Omniverse S3
MDL modules after the scene-reload gap, during the final render/camera period;
the timing is correlated, but does not prove those missing materials caused
the long first physics step. No change that skips or replaces this step is
safe to ship without a controlled comparison because it may alter scene
settling and task behavior.

These two runs confirm that runtime optimization is not complete: first-frame
latency is still 15–18 minutes. App launch and environment creation are only
about 33–34 seconds combined; the first physics step after loading the scene
dominates the wait. Scene pose reuse is not a useful optimization for this
case until that wait is understood.

## Same-layout scene file and reuse experiment (2026-10-10)

An opt-in experiment was run directly with the simulator on GPU 0 for
`deposit_coin`, seed 0, layouts 0 and 1. It used no LLM, external policy
session, or upload. For each process it exported the current USD root layer,
opened that file as an independent USD stage, then reset the same live
environment once with a full scene rebuild and once by restoring saved poses
without deleting the existing prims. This tests stage parsing and same-process
reuse; it does not test starting a fresh Isaac process directly from the
exported file.

| Run ID | Layout | Initial `scene_reload_envs` | Same-process full reset | Same-process pose-reuse reset | USD export / reopen |
| --- | ---: | ---: | ---: | ---: | ---: |
| `scene_cache_reuse_trial_20261010c` | 0 | 4.41 s | — | 12.28 s | 5.4 / 5.1 ms |
| `scene_cache_reuse_trial_20261010d` | 1 | 4.36 s | — | 12.76 s | 7.6 / 7.1 ms |
| `scene_cache_reuse_control_20261010e` | 0 | 4.84 s | 13.02 s | 13.80 s | 5.9 / 5.1 ms |

The best controlled comparison is the final row: two successive resets in the
same process took 13.02 s with a full rebuild and 13.80 s with pose reuse. The
reuse reset was not faster in this sample. It preserved the same four managed
object prims, passed layout stability, and produced valid three-camera RGB
frames. Reset warmup and layout stability still run in both modes, so skipping
the asset rebuild did not remove those costs. The first full `env_reset` in
this row took 25.33 s; the same-process control and reuse resets took about 13
seconds each.

The exported files were about 70 KB and contained 595–597 traversable prims.
Opening the root layer took 5–7 ms, but this is only USD parsing of the layer
and its references. It excludes PhysX object registration, camera rendering,
and a complete fresh-process environment setup. This result does not establish
that full scene-file reload would be faster than the production path.

Across these three runs, the long 710–959 second scene reload did not
reproduce: full `scene_reload_envs` took 4.36–4.84 seconds, and current client
logs had no invalid Omniverse S3 MDL warnings. This suggests the earlier
minute-scale stall was transient or depended on a different cache/resource
condition; its cause remains unproven. These runs do not justify deploying a
scene-reuse optimization yet. A persistent simulator worker could still avoid
the roughly 13–15 seconds of AppLauncher startup and 20–21 seconds of
environment creation for later tasks, but that lifecycle change was not
tested here.

The opt-in experiment code is enabled only by setting
`ROBOICL_SCENE_REUSE_EXPERIMENT=1`; set
`ROBOICL_SCENE_REUSE_CONTROL_RESET=1` to add the same-process full-rebuild
control before the reuse reset. It is not part of normal API runs.

## Follow-up implementation: scene-load attribution

Diagnostic runs now emit nested spans for `reload_background`,
`reload_lights`, `reload_ground`, `reload_room`, `reload_table`, stale-object
relocation, object clearing/spawning, and each `scene_object_create`. Object
creation events include the object type, environment index, prim path, and
asset basename; they omit private asset roots. These spans are enabled only
when diagnostics are active and do not change the normal scene/reset path.
They are intended to identify whether a repeated long `scene_reload_envs`
wait is in a specific USD reference, MDL/material setup, or other reset step.
Each pending scene object's `initialize()` call is also timed independently,
since a current live probe showed room/table/object creation returning quickly
while the enclosing `reload_envs` call remained open.

No scene reuse or pre-export fast path is enabled in production. The controlled
same-process sample showed pose restoration was 0.8 seconds slower than a full
rebuild, while reopening the exported USD measured parsing only. The next
optimization decision should use the new component spans if the long stall
returns. A per-GPU persistent Isaac worker remains a separate lifecycle
project; its expected benefit is avoiding roughly 13–15 seconds of app launch
and 20–21 seconds of environment creation per later task, and it requires
explicit validation of environment teardown, task/layout switching, GPU
memory retention, cancellation, and worker recovery before enabling it.
