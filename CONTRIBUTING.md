# Contributing

Bug reports and questions are welcome. Please open an
[issue](https://github.com/vocaro/queuekeeper/issues) first for anything larger than a small fix, so
we can agree on the change before you spend time on it.

## Changing Queuekeeper

Read [AGENTS.md](AGENTS.md) and the [developer guide](doc/developer_guide.md#working-on-queuekeeper).
You need macOS and Python 3.14 or newer. Run tests through Queuekeeper's own admission:

```sh
python3 scripts/qk unittest -s tests integration.test_merge_queue.Landing
python3 scripts/qk run gate
```

## Pull requests

This repository lands on `main` through its own merge queue, not by merging pull requests. Open a
pull request as usual; once it is accepted, a maintainer lands its commits through the queue, with
your authorship kept, and closes the pull request. No hosted CI runs on pull requests: the queue's
gate, on the maintainer's Macs, tests your commits when they land.

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
