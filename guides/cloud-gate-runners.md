# Cloud gate runners

A rented Apple Silicon Mac with SSH can serve as an overflow host for the merge queue. The
queue already sends the exact batch and toolkit commits, asks the destination to admit the
lane, and collects its verdict and results. A persistent Mac can keep build products and
simulator templates warm across runs. The worker still owns the push to `main`.
[Build Mac configuration](shared-host.md#build-macs-for-merge-queue-gates)

For a first dedicated-host comparison, the 32 GB Scaleway M4 costs €6.96 for its minimum
24-hour lease. NoMac offers shorter metered sessions when overflow is intermittent. Compare both
with an added local Mac if capacity is needed most days.

## Provider comparison

Published rates reviewed November 10, 2026. Amounts are in the provider's listed currency
and exclude taxes, optional storage, and network charges.

| Provider | Published compute price | Fit for the queue |
| --- | --- | --- |
| Scaleway Apple Silicon | €0.22/hour for M4 16 GB and 256 GB, or €0.29/hour for M4 32 GB and about 1 TB. The 24-hour minimum costs €5.28 or €6.96 respectively. | Dedicated SSH host for a day of testing with warm checkouts and simulator runtimes. [Prices](https://www.scaleway.com/en/pricing/apple-silicon/), [SSH and minimum lease](https://www.scaleway.com/en/docs/apple-silicon/quickstart/) |
| NoMac Session | $0.90/hour of ready time, billed in whole minutes from prepaid credit. A $10 pack buys about 11.1 hours. | Disposable macOS VM with SSH and CLI or API lifecycle controls. Measure availability and the cost of restoring the toolchain and caches before treating it as overflow capacity. [Pricing](https://nomac.app/docs/pricing), [CLI](https://nomac.app/docs/cli) |
| Roundfleet | $99/month for an M4 with 16 GB and 256 GB. | Persistent dedicated Mac. Check disk use with the project's Xcode and simulator runtimes before choosing the base configuration. [Pricing](https://www.roundfleet.com/pricing), [SSH access](https://www.roundfleet.com/) |
| MacStadium | $149/month for an M4 with 16 GB and 256 GB. | Persistent dedicated Mac with a flat monthly bill. [Pricing](https://macstadium.com/pricing) |
| GetMac | $33.99/month for 3,000 base M4 minutes and up to three concurrent VMs. M4 Pro consumes twice the minutes. | Actions runner. The queue would need a workflow dispatcher that can test its exact batch commit and return the verdict. [Pricing](https://getmac.io/pricing), [Runner setup](https://getmac.io/docs/github-runners/getting-started) |
| GitHub-hosted macOS | $0.062/minute on the standard runner beyond included usage, or $0.102/minute on the larger Apple Silicon runner. | Billing baseline for hosted Actions. A 30-minute job costs $1.86 or $3.06 at those rates. [Runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing) |

At equal billed runtime, 100 half-hour jobs use $45 of NoMac credit or $186 of standard
GitHub-hosted macOS compute beyond the included allowance. That comparison assumes 50 billed
hours on each service. Checkout, cache setup, retries, and runner speed can change both the
bill and time to a verdict.

The repository's current operating policy keeps tests on the owner's Macs. A cloud pilot
requires an explicit policy decision before sending private source or test results to a
provider. The Actions rows are comparison points under that policy.
[Shared resource rules](https://github.com/vocaro/vocaro-engineering/blob/main/agents/core.md#shared-resources)

## Measure the queue impact

Landed batches with recorded gate durations from November 3–9, 2026, provide a starting
workload. EPUBLib recorded 69 batches with a median of 20 minutes and a maximum of 75 minutes.
BreezEPUB recorded 41 batches with a median of 9 minutes. The sample counts each landed batch
once and excludes queue wait, failed attempts, and canceled runs.

1. Compare one eligible batch on a current Mac and the candidate host at the same commit.
   Match the Xcode build, simulator runtimes, and required tools. Check the verdict and
   executed test counts before interpreting timings.
2. Measure cold and warm dispatches. Record host startup, checkout and simulator preparation,
   time from dispatch to a usable verdict, and billed runtime. Include canceled or retried
   runs in the cost.
3. If the host qualifies, compare queue admission wait and time from enqueue to landing
   before and during overflow use. Look at the busiest periods and keep canceled speculation
   separate from batches that landed.

Ordinary remote gate lanes can use the configured build Mac list. A single project can run
one stacked gate alongside its current batch. More hosts can let different projects gate
concurrently while that per-project limit stays in effect. Hardware-specific and foreground
lanes retain their placement rules.
[Stacked gates](maintenance.md#the-merge-queue), [Host placement](shared-host.md#which-mac-runs-a-lane)
