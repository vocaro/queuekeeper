# Porting from vocaro-engineering

Queuekeeper is being split out of the private toolkit vocaro/vocaro-engineering. The plan, its
decisions and its findings are in vocaro/vocaro-engineering#403. This note says where the port
stands and what is left. Delete each item as it is done, and the file when the list is empty.

## Where it stands

- The code and its history were extracted from the toolkit's `main` at `4bad68c4` with
  `tools/extract-from-toolkit.sh`, which keeps the paths in `tools/extract-paths.txt`. Run it again
  and merge its result to take the toolkit's later changes; the script says how.
- Every module moved, unchanged, into `src/python/queuekeeper` (the core) or
  `src/python/queuekeeper/apple` (the Apple extension). No import is rewritten yet, so 22 of the
  46 modules do not import, and every test module but `tests/test_queuekeeper_imports.py` fails to
  import, because `tests/host_isolation.py` imports `vocaro_engineering`.
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

## Toolkit files here only to copy from

The toolkit keeps these. The core copies the little it uses, then the file is deleted here and its
name taken out of `BORROWED` in `tests/test_queuekeeper_imports.py`, which until then counts an
import of one as an import of the toolkit.

| File | What the core takes |
|---|---|
| `evidence.py` | The lane-result vocabulary: `UNMEASURED`, `unmeasured_tests`, `counted`, `repeats_asked`, `skipped_tests`, `RAN`, `PYTEST_SUMMARY`. The core owns it, and the toolkit's `evidence` imports it |
| `landing.py` | The four proofs: `contained`, `negated_claims`, `own_merges`, `brought_in`, with `proof`, `changes`, `hunks`, `same_change` and `resolutions` |
| `toolkit_copy.py` | What `budgets` and `remote` use |
| `stale_modules.py` | What `parallel` uses |

## Import ties to cut

`python3 tests/test_queuekeeper_imports.py --report` prints the current list. At the layout commit:

| Kind | Count |
|---|---|
| Core imports toolkit | 30 |
| Core imports Apple | 21 |
| Apple imports toolkit | 14 |
| Allowed, but written under a toolkit name or place (`old name`) | 34 |

- The old names are mechanical: `vocaro_engineering.processes` becomes `queuekeeper.processes`, and
  the extension's `from . import remote` becomes `from .. import remote`.
- `processes`, `progress`, `lane_wait`, `lane_progress`, `resources` and `remote` import each other
  inside functions and form one cycle. A module with such imports loads today and fails when the
  function runs.
- The tests import `vocaro_engineering` throughout, `tests/host_isolation.py` first. The import test
  goes back to importing `host_isolation`, as every test module does, once that helper imports.

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
