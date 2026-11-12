# Porting from vocaro-engineering

Queuekeeper is being split out of the private toolkit vocaro/vocaro-engineering. The plan, its
decisions and its findings are in vocaro/vocaro-engineering#403. This note says where the port
stands and what is left. Delete each item as it is done, and the file when the list is empty.

## Where it stands

- The code and its history were extracted from the toolkit's `main` at `4bad68c4` with
  `tools/extract-from-toolkit.sh`, which keeps the paths in `tools/extract-paths.txt`. Run it again
  and merge its result to take the toolkit's later changes; the script says how.
- Every module is in `src/python/queuekeeper` (the core) or `src/python/queuekeeper/apple` (the Apple
  extension), and every import the rules allow is written for this layout. 34 of the 46 modules
  import. The other 12 wait for an import that breaks a rule to be cut: `disk`, and through it
  `remote`, `merge_queue`, `lane_report`, `cli` and `apple.keychain`; `apple.simulators.speech`, and
  through it `pool`, `matrix` and `apple.run_results`; and `apple.simulators.duo_pose` and `template`.
- `tests/host_isolation.py` imports `cli` and `remote`, so no test module that imports it runs yet.
  Five that need no host isolation run without it for now: `test_queuekeeper_imports`,
  `test_lane_results`, `test_landing_proofs`, `test_git_state` and `test_shims`. Each says so, and
  imports `host_isolation` again once that helper imports.
- `tests/test_queuekeeper_imports.py` holds the rules: the core imports neither the toolkit nor the
  extension, and the extension never imports the toolkit. `tests/known_import_violations.txt`
  lists where the code breaks them today, and that list only shrinks.
- `pyproject.toml` declares the `queuekeeper` and `qk` commands as `queuekeeper.cli:main`, which
  is still the toolkit's whole `eng` command.

## Files still to split

These came whole and sit on the side that gets most of each.

| File | Now in | Still to do |
|---|---|---|
| `remote.py` | core | Device leases, runtime matching and Xcode go to the extension. "Second Mac" becomes a host list |
| `offload_host.py` | core | Reaping, runtime images, Xcode and crash dialogs become a host-side module of the extension that the agent loads |
| `lane_report.py` | core | The Swift Testing and XCTest formats go to the extension |
| `lane_coverage.py` | core | The readers for `swift test`, `xctest`, `xcodebuild test` and `simulator-tests` go to the extension |
| `disk.py` | core | Keep lane results by commit and the worker's and hosts' checkouts. Simulators, runtimes, DerivedData, the SwiftPM cache and kept app builds go to the extension. Worktrees, scratch folders, failure records and evidence archives stay in the toolkit's `eng disk`: delete them here |
| `cli.py` | core | Keep the lane runner, `admit` and the commands that leave `eng` (`run`, `admit`, `admissions`, `remote`, `queue`, `land`, `unittest`, `spelling`, and `simulator-tests`, `simulator-pool`, `xctest`, `results` through the extension). Delete the rest, which stays `eng` |
| `config.py` | core | Keep a loader for `queuekeeper.json` with the queue, lane and host keys. Delete the rest, which stays the toolkit's |
| `budgets.py` | core | The device half of its doctor warnings goes to the extension; its use of `release` goes |

## Copies of toolkit code

The toolkit keeps these files, and the core holds a copy of the little it uses of each, unchanged.
`tools/extract-paths.txt` still names the four files, so a later extraction that changed one shows as
a conflict with its deletion here: port the change to the copy by hand, then keep the file deleted.

| Toolkit file | Copy here | Tests |
|---|---|---|
| `evidence.py` | `lane_results.py`: the lane-result vocabulary. The core owns it, and the toolkit's `evidence` is to import it | `test_lane_results.py`, from `test_core.py` |
| `landing.py` | `landing_proofs.py`: `contained`, `proof`, `own_merges`, `brought_in`, `negated`, `negated_claims` and what they rest on | `test_landing_proofs.py`, from `test_closeout.py` |
| `toolkit_copy.py` | `git_state.py`: `git`, `behind`, `last_landed`, `worktrees` | `test_git_state.py`, new |
| `stale_modules.py` | `shims.py`: the environment that puts the build-tool wrappers first on a lane's PATH | `test_shims.py`, from `test_stale_modules.py` |

Still open:

- `cli.py` still imports `evidence`, `toolkit_copy` and `stale_modules`, and `disk.py` `landing`, as
  toolkit modules; they change to the copies when those two files are split.
- The wrappers `bin/shims/xcodebuild` and `bin/shims/swift`, and the rebuild they make when a module
  cache is stale, are not here. `shims.SHIMS` names a folder that does not exist, so a lane's own tools
  run. They wrap Apple's build tools: decide whether they come to the extension.
- The case of `test_core.py`'s `EvidenceTests` that goes through `cli.judge` comes with the lane runner.
- The contract with the toolkit: a commit the queue lands past a conflict, by keeping both sides, must
  read as landed by the `resolved` method in the toolkit's `landed.sh`.

## Import ties to cut

`python3 tests/test_queuekeeper_imports.py --report` prints the current list. Now:

| Kind | Count |
|---|---|
| Core imports toolkit | 22 |
| Core imports Apple | 21 |
| Apple imports toolkit | 4 |
| Allowed, but written under a toolkit name or place (`old name`) | 0 |

- Core imports toolkit: `cli` 15 and `disk` 5, which go when those files are split; `budgets` imports
  `release`, and `parallel` `failure_records`, inside a function.
- Core imports Apple: `cli` 12, `remote` 5, `disk` 3 and `merge_queue` 1, for the extension's hook points.
  `cli.py` and `disk.py` still name the extension's modules as their own (`from . import devices`).
- Apple imports toolkit: `keychain` imports `host_context`, `simulators.duo_pose` and
  `simulators.template` import `support`, and `simulators.speech` imports `voiceover.activation`.
- `processes`, `progress`, `lane_wait`, `lane_progress`, `resources` and `remote` import each other
  inside functions and form one cycle.
- Tests still import five toolkit modules by the toolkit's name: `release`, `failure_records`,
  `evidence_offload`, `ignored` and `host_context`.

## Tests

All tests are in `tests/`, flat, because each imports `host_isolation` from beside it.

- **Extension:** `test_simulator_pool`, `test_simulator_reap`, `test_simulator_matrix`,
  `test_simulator_template`, `test_simulator_runtime_identity`, `test_lifecycle`, `test_speech`,
  `test_duo_pose`, `test_devices`, `test_device_pass`, `test_capture_approvals`,
  `test_xctest_shards`, `test_foreground`, `test_crash_alerts`, `test_app_registrations`,
  `test_notification_center`, `test_keychain`, `test_voiceover_state`, `test_voiceover_guard`,
  `test_app_builds`, `test_run_results`.
- **To split:** `test_disk`, with `disk.py`; `test_lane_coverage`, whose Apple cases go to the
  extension.
- **Core:** the rest. `test_processes`, `test_progress`, `test_waiting`, `test_leftover_stops` and
  `test_slow_listings` test moved modules and came along, though the plan's lists leave them out;
  `test_leftover_stops` also imports the simulator pool.
- **Not brought, and needed:** `test_core` holds the admission tests (`ResourceTests`, `CpuBusyTests`,
  `AdmissionLogTests`, `UsageTests`, `RecoveryTests`, `LaneLabelTests`), about 4,000 lines that test
  `resources`, beside tests of what stays.
- **Not brought:** `test_core`, `test_launcher`, `test_package_paths` and `test_setup` test `cli`
  and `config` beside toolkit modules; the cases about the lane runner, `admit` and the queue's
  config keys are still to be found and copied. `test_git_ssh` tests `bin/eng-git-ssh`, and
  `test_stop_signals` the probes in `src/python/StopSignals`; neither has a row in the plan.

## Not started

- Queuekeeper's own names: the state directory, ref prefix, worker home, environment variables and
  the launcher's bundle ID (the plan's "Names to replace").
- `queuekeeper.json` and its loader, the extension's hook points, and a `scripts/qk` adapter.
- The guides: keep "The merge queue" from `guides/maintenance.md`, and the admission, second-Mac,
  Local Network, simulator and device sections from `guides/shared-host.md`; delete the rest.
- `src/objc/Simulator/duo-pose-helper.c` belongs to the extension (`simulators/duo_pose.py` builds
  it); `src/objc/QueueLauncher` to the core. Neither has moved within `src/objc`.
- A temporary `engineering.json` with a `gate` lane, so that this repository lands through the
  toolkit's queue until cutover.
