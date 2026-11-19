# Kept app builds

`qk app` keeps the last good build of a project's app on this Mac and runs it: the newest
landed app opens on the Mac at once, and installs and launches on the paired iPhone or iPad
with one command. The code is `plugins/apple/src/queuekeeper_apple/app_builds.py`.

## Why the gate's builds are not kept

The landing gate does build the app, but nothing it leaves is a build to run:

- Its lanes build for tests (`xcodebuild test`), into DerivedData inside the queue's checkout,
  which the next batch builds over.
- The batch's commit may not be the one pushed: a push that meets a newer main carries the batch
  onto it.
- A lane may run on another Mac, so its products are not on this Mac.
- No gate builds for a physical device.

So after the merge queue pushes a gated batch it starts `qk app keep --after-landing` in the
background, for the pushed commit. That command builds the app with `xcodebuild build`, as an
ordinary build, and keeps the product. A good build is a commit that landed through the gate and
then built. A build that fails keeps nothing, and the build kept before it stays the one that
runs. A docs-only or ungated landing keeps no build.

## Configuring a project

engineering.json's `app` says how to build the app:

```json
"app": {
  "project": "Name.xcodeproj",
  "configuration": "Debug",
  "mac": {"scheme": "Name"},
  "ios": {"scheme": "Name", "settings": {"DEVELOPMENT_TEAM": "TEAMID"}}
}
```

- `project` or `workspace` names the Xcode project or workspace (one of them), as a path inside
  the checkout. `directory` is where it is and where the build runs (by default the checkout's
  top).
- `prepare` is a command run in `directory` before each build, such as
  `["xcodegen", "generate", "--quiet"]` for a project generated from project.yml, or a list of
  commands run in order, such as that one and then one that seeds the generated workspace's
  Package.resolved from the project's own (BreezEPUB). Each is an argument list, never shell
  source; the first to fail stops the build. A project that already has a script for generating
  its project names that instead (StudyWright's `["../scripts/app.sh", "generate"]`, which also
  stamps its signing team into the project).
- `mac` and `ios` each give the `scheme` that builds the app. `product` names the built app, such
  as `Name.app`, when the build makes more than one. `settings` are build settings for
  xcodebuild's command line.
- `configuration` defaults to Debug.
- `afterLanding` says whether the queue keeps that platform's build after each landing: by
  default the Mac build is kept and the device build is not. A device build is made when a
  device asks for it, as described in the next section. Each keep is one admitted build on
  this Mac, outside the gate's time. It waits behind a whole-host gate, and while it holds its
  slot the next gate waits for it.

The Mac build runs for `generic/platform=macOS`. The device build runs for
`generic/platform=iOS` with `-allowProvisioningUpdates`, so it needs the project's signing team.
Each app extension needs a development profile for its own bundle identifier: a team's wildcard
profile matched BreezEPUB's app but not its Thumbnail and Preview extensions (2026-11-06,
[vocaro-engineering#206](https://github.com/vocaro/vocaro-engineering/issues/206)).
`-allowProvisioningUpdates` creates a missing profile only when an Apple Account is signed in to
Xcode on that Mac. With none (`defaults read com.apple.dt.Xcode DVTDeveloperAccountManagerAppleIDLists`
lists no account), the build stops at "requires a provisioning profile", and the fix is the
owner's: sign in under Xcode > Settings > Accounts, or create the profiles in the developer
account.

### Signing

Every target the app builds, extensions included, must sign automatically. Only then does
`-allowProvisioningUpdates` find or create its profile. A target that signs manually and names
no profile fails with "requires a provisioning profile", and `qk app` says so. XcodeGen writes
no `CODE_SIGN_STYLE`, so a target generated from a project.yml that sets none signs manually, and
Xcode shows Automatically manage signing unchecked. Set `CODE_SIGN_STYLE: Automatic` in the
spec's `settings.base`, as BreezEPUB does, or on each target, as StudyWright does. Choosing the
team in Xcode is no fix, since `prepare` regenerates the project before each build. On
2026-11-07 BreezEPUB's app signed under manual style with the team's wildcard profile, while
its two Quick Look extensions failed. Under automatic style all three used the wildcard
profile (vocaro-engineering#206).

When this checkout's engineering.json has no `app` but origin/main's has one, `qk app` says
how far behind the checkout is. Update it, or run from a worktree at origin/main.

## Running it

```bash
python3 scripts/qk app run
```

`qk app run` opens the newest kept Mac build on this Mac. First it fetches origin's main. If
the newest kept build is older than main, it builds main and runs that build. If that build
fails, it runs the newest kept build and says how many commits behind main that build is.
`--no-build` runs the newest kept build as it is. When another copy of the app is running from
somewhere else, such as a gate's or a session's test host, the command refuses and names that
copy. `--replace` quits that copy first; quitting a gate's test host fails that gate's run.
Opening an app on this Mac uses the owner's screen, so run it only when the owner asks.

```bash
python3 scripts/qk app run --ipad
```

`--ipad` and `--iphone` install the device build on that paired device and launch it, ending a
copy already running there. The build is made the same way as the Mac's, and a device build
that is current is used as it is. The install and launch run as a device pass under the
device's reservation ([device testing](https://github.com/vocaro/apple-engineering/blob/main/guides/device-testing.md), [device reservations](devices.md#device-reservations)):

- Either device is taken in turn, first come first served, or the session's own reservation is
  used and stays held afterward. A second `qk app run` for the same device waits behind the
  first, then installs and launches again.
- The command checks no approval (owner's decision, 2026-11-06). The iPad carries the owner's
  standing approval; the iPhone is his personal phone, so a session runs `--iphone` only when he
  has asked for it or agreed.
- The owner's notifications are silenced for the pass and put back at release. The app is left
  running on the device for the owner to use.
- The first iPhone run, on 2026-11-07, built Pagepour's device build cold (18 minutes) and then
  installed and launched it. A banner came through within seconds of the silencing shortcut running, which
  the Mac cannot confirm on the phone. The pass marked that try unmeasured and installed and
  launched again, seeing none ([notifications during a run](devices.md#notifications-during-a-run)).
  Otherwise the iPhone ran as the iPad had.
- `--owner` names the session, which defaults to `QUEUEKEEPER_DEVICE_OWNER`, then the agent
  session, then the account.

`qk app keep` builds and keeps origin/main, or the commit `--commit` names, for each platform
or those given with `--platform`. `qk app status` lists the kept builds and how far each is
behind origin/main.

## The store

Everything is under `~/Library/Application Support/queuekeeper/apps/NAME/`, outside
every checkout and outside Xcode's DerivedData, so that every session and the queue share it:

- `source/` is a detached worktree of the project, locked with the reason "qk app's source
  checkout for kept app builds". Each build checks out its commit there, and `qk disk` keeps a
  locked worktree.
- `vocaro-engineering` links to the shared toolkit checkout, where the project's `toolkit.path`
  finds it from `source`: a project that builds the toolkit's Swift packages names them by that
  path, as the queue's checkout finds its toolkit copy beside it.
- `derived/PLATFORM/` is each platform's DerivedData, kept between builds so that each build is
  incremental. `qk disk` lists it as a candidate once it is idle, and keeps it while a keep
  holds the store's lock. Removing it makes the next keep a full build.
- `kept/PLATFORM/SHORT/Name.app` holds the newest two builds of each platform, with `kept.json`
  recording each one's commit, bundle identifier and build time. `qk disk` lists them as kept.
- `logs/` holds each build's log, and `lock` is held by a keep while it builds, so that only one
  build at a time uses `source` and `derived`. `qk app run` waits for a keep under way rather
  than building beside it.

A build runs through `qk admit --here`, on this Mac, since its product is this Mac's.
