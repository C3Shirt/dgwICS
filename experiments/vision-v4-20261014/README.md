# Vision v4: implementation and real CLI evidence

English prompts are in Python. Inputs contain an independent industrial description, official per-instance knowledge, expected relations and current native pose; outputs contain only anomalies.

**Acceptance remains incomplete.** A strict emitter-only repair succeeded, but a known injected feeder offset was later missed and auto-labelled scene_verified. That run is explicitly rejected as a correctness example. Do not use automatic status or this archive as training gold.

| Run | Framework result | Calls | Seconds | Review |
| --- | --- | ---: | ---: | --- |
| [issue4-vision-v4-columns-20261014-001](trajectories/issue4-vision-v4-columns-20261014-001/result.json) | scene_verified | 1 | 10.049 | Recorded outcome; not independently labelled accuracy. |
| [issue4-vision-v4-comparison-20261014-001](trajectories/issue4-vision-v4-comparison-20261014-001/result.json) | visual_comparison_recorded | 4 | 29.852 | Recorded outcome; not independently labelled accuracy. |
| [issue4-vision-v4-feed-fault-20261014-001](trajectories/issue4-vision-v4-feed-fault-20261014-001/result.json) | scene_verified | 4 | 37.753 | Recorded outcome; not independently labelled accuracy. |
| [issue4-vision-v4-feed-fault-20261014-002](trajectories/issue4-vision-v4-feed-fault-20261014-002/result.json) | scene_verified | 3 | 35.31 | Recorded outcome; not independently labelled accuracy. |
| [issue4-vision-v4-feed-fault-20261014-003](trajectories/issue4-vision-v4-feed-fault-20261014-003/result.json) | needs_attention | 4 | 44.993 | scene model HTTP 402 |
| [issue4-vision-v4-fresh-layout-20261014-001](trajectories/issue4-vision-v4-fresh-layout-20261014-001/result.json) | needs_attention | 7 | 96.875 | grid_offset is for ancillary controls/supports, not feed or detection geometry |
| [issue4-vision-v4-fresh-layout-20261014-002](trajectories/issue4-vision-v4-fresh-layout-20261014-002/result.json) | scene_verified | 1 | 10.161 | Recorded outcome; not independently labelled accuracy. |
| [issue4-vision-v4-negative-check-20261014-001](trajectories/issue4-vision-v4-negative-check-20261014-001/result.json) | scene_verified | 1 | 12.85 | known injected feeder offset was missed; automatic scene_verified is not accepted as correctness |
| [issue4-vision-v4-positive-check-20261014-001](trajectories/issue4-vision-v4-positive-check-20261014-001/result.json) | scene_verified | 1 | 10.642 | Recorded outcome; not independently labelled accuracy. |
| [issue4-vision-v4-protocol-20261014-001](trajectories/issue4-vision-v4-protocol-20261014-001/result.json) | protocol_probe_recorded | 6 | 33.27 | Recorded outcome; not independently labelled accuracy. |

535 Python tests pass (publication checkout: one reference-scene test skipped); 12 dashboard tests pass. Forty layout tests use synthetic adapters and are not live acceptance.

The six protocol trials froze the original system/user text and image bytes. Four returned []; each combination was sampled once, so no cause is proven. Full requests/replies and metadata are retained. Raw HTTP/SSE packets are not captured by the client.

Images and .factoryio scenes retain their original bytes. Every capture keeps its own sidecar. Local publication PNGs use hardlinks; identical images are also deduplicated by Git blobs. No runtime framework-source copies are introduced.

Machine config snapshots are excluded; secrets, private model hosts and local paths are scrubbed. Each file has both original and public hashes in [publication-manifest.json](publication-manifest.json).

The changed prompts were iterated during these runs; execution.json retains each run's own source hashes. The final sources are in this PR. Both failed trials and later changes are retained; no trial is silently replaced.

No PLC execution, material transport acceptance or independent accuracy statistics are claimed. See [validation summary](validation/issue4-v4-validation-summary.json).
