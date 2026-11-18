# Scheduling

What Queuekeeper's scheduling is for. This guide states the goals the schedule serves and their
order, the resources it shares out and the work that needs them, the rules in force with the
decision that set each, and the decisions still open. It is where a change of policy is weighed
against stated goals instead of against the latest incident. It changes no behavior. How each
mechanism works, with its messages and measurements, stays in the guides this one cites:
[heavy-work admission][admission], [which Mac runs a lane][placement] and
[the merge queue][queue].

Here `#N` is an issue of vocaro/queuekeeper and `ve#N` one of vocaro-engineering, where these rules
were decided before Queuekeeper had a repository of its own. A date beside a rule is the day it was
decided, or the day of the incident that led to it; the cited guide or issue says which, and whose
decision it was. "This Mac" is the Mac that work starts on, the owner's workstation, and "another
Mac" is one of [the farm's][farm] others.

## Goals, in order

The owner's direction, 2026-11-17: "optimize for throughput without starving jobs", "the
scheduling should take into account the CPU load limits that are defined per host", "the scheduler
should try to balance the load where possible", and "the scheduler/admitter should treat all nodes
as equivalent except for per-node configs".

The order below is a reading of that direction and of the decisions under
[Policies in force](#policies-in-force), and the owner confirmed it on 2026-11-17
([D1](#d1-is-the-order-of-goals-right)). Where two goals conflict, the earlier one wins.

1. **A verdict is true.** A pass, a failure or a timing says something about the commits, never
   about what else the Mac was doing. Work that could not be measured ends unmeasured and runs
   again; it is neither passed nor sent back. This is why a gate keeps its Mac to itself ([ve#196]),
   why a timed measurement waits for a quiet Mac ([#121]), and why a run with no verdict blames no
   entry.
2. **Each Mac stays within its own limits.** A Mac's limits file says how much runs there at once
   and how busy its CPU may read before more is added. The limits exist because ten unbounded
   builds panicked a Mac (2026-10-14), and they differ by Mac because this Mac is also the owner's
   workstation (2026-11-07).
3. **Nothing waits without bound.** Requests are served in arrival order. A request that has
   waited is not passed again and again by later or lighter work ([ve#200], [ve#174], [#160]).
4. **Landings flow, and no Mac idles while work waits.** This is the objective that the first
   three bound: entries landed per hour, across every project's queue. Work goes to a Mac that has
   room ([ve#168]), spread across the Macs where it can run on more than one (2026-11-17), and a
   queue does in parallel whatever it safely can ([ve#241]).
5. **A session's own run starts soon.** The latency of a hand-run lane matters least. It is served
   by spare capacity and by shortcuts that cost nobody a place, never by priority ([ve#200]).

Two rules stand beside the order. **The owner is not the scheduler.** Queuekeeper chooses the Mac
and the moment, a session never asks him which Mac to use ([ve#168]), and a queue holds for him only
over a cause that only he can fix. **No Mac is special.** Admission and placement treat every Mac
alike, and Macs differ only by their own settings: their CPU limits and which kinds of lane each
may run (2026-11-17). The rules in force do this only in part
([D7](#d7-no-mac-is-special)).

Read as one sentence: land as much as the Macs can verify truthfully, within each Mac's limits, in
arrival order with bounded waits, and only then make a single run start sooner. The rules in force
fall short of this in one place: the foreground rule still names this Mac, and which kinds of work
a Mac takes is not yet its setting. That is decided under
[decisions](#decisions) and waits to be built. Three others ended on 2026-11-17: placement that
ordered the Macs without balancing them (rule 14), a CPU limit that bounded only the slots
beyond the guaranteed ones (rule 3), and a merge queue that kept the first gate of a pair on this
Mac and the second off it (rules 24 and 25).

## Resources

Each row is something only so much work can have at once. The first five are one Mac's
[admission][admission]; the rest are taken beside it.

| Resource | Counted per | How much | Who takes it |
| --- | --- | --- | --- |
| Guaranteed slot | Mac | `slots`, 3 by default | any admitted work while the CPU's busy share is under that Mac's `busyLimit`, and the first job on a Mac with nothing admitted whatever it reads |
| Slot beyond the guaranteed | Mac | up to `slotsMax`, half the cores by default | one-slot work, one at a time, while the CPU's busy share is under that Mac's `busyLimit`, memory pressure is normal, and `settleSeconds` (30) have passed since the last admission |
| Whole host | Mac | one lane | a whole-host lane: every guaranteed slot, under the same CPU limit, and nothing else admitted while it runs |
| Exclusive host | Mac | one request | exclusive work: every slot up to `slotsMax` and the model, started once the CPU reads quiet (under 25%) or after 300 s |
| Model and GPU | Mac | one run | a model run, with one slot ([model admission][model]) |
| Unlocked screen | Mac | a state, not a lock | a lane that says `"unlockedScreen": true` ([screen state][screen]) |
| Foreground | Mac marked for it | one holder | a foreground lane, taken before admission ([foreground][foreground]) |
| Physical device | this Mac | one holder each, in turn | a device lane, which says `here` ([devices][devices]) |
| Pool simulator | project and Mac | the pool's members | a simulator lane ([simulator pool](simulator-pool.md)) |
| Checkout | project, on another Mac | four | any run sent there |
| Mac | farm | those listed, reachable and not paused | every lane that does not say `here` |
| `main` | repository | one worker, one batch gating, one more gate beside it | a queue's entries ([the merge queue][queue]) |

**Each Mac's limits are its own.** `slots`, `slotsMax`, `busyLimit` and `settleSeconds` come from
that Mac's `admission-limits.json`, and a key it leaves out keeps the default. The values live on
each Mac and are not copied here: `qk admissions limits` prints one Mac's with where each comes
from, and `qk admissions usage` prints every Mac's beside its reading now. Since 2026-11-07 the
owner sets another Mac's CPU limit higher than this Mac's. Setup does the same for a new
installation: it writes a `busyLimit` of 70 for the Mac it runs on, and the default, which a Mac
added to the farm keeps, is 90 (2026-11-17, [D7](#d7-no-mac-is-special)).

**What the CPU limit bounds, and what it does not.** `busyLimit` decides whether a Mac admits more
work: any slot, guaranteed or beyond, opens only under it, except for the first job on a Mac with
nothing admitted (rule 3, [D4](#d4-does-a-macs-cpu-limit-bound-every-admission)). It decides the
same for placement: whether a Mac has room, and which of the Macs with room is chosen (rules 14 and
16). And it decides when a queue's worker runs a lane again after a run with no verdict. It bounds
when work starts and not what it does afterward: each slot holds a lane however many processes it
starts ([#170]), nothing is preempted (rule 12), and work that never asks admission is not counted
at all ([load the queue cannot see][unseen]), though its load now delays every admission but the
first. Until 2026-11-17 a guaranteed slot admitted work at any reading and a whole-host lane
started on free guaranteed slots at any reading ([#12]): that day `qk admissions usage` read this
Mac 92% busy against its limit of 70% and another Mac 100% busy against its 90%, with every
guaranteed slot of both held.

## Work classes

| Work | Declared by | Needs |
| --- | --- | --- |
| Preflight check, docs check | `queue.preflight`; the queue's own | nothing when the worker or `qk land` runs it, for up to a minute; one slot as a lane |
| One-slot lane or command | `"resource": "heavy"` or none; `qk admit` | one slot |
| Model run | `"model": true`; `qk admit --model` | one slot and the model |
| Whole-host gate | `"wholeHost": true` | the whole host |
| Timed measurement | `"resource": "exclusive"`; `qk admit --exclusive` | the exclusive host, read quiet |
| Self lane | `"resource": "self"` | nothing itself: its script admits each part, all on one Mac |
| Screen-reading lane | `"unlockedScreen": true` | its slots, on a Mac whose screen reads unlocked |
| Foreground lane | `"foreground": true` | a Mac's foreground, then its slots |
| Device lane | `"here": "REASON"` and a reservation | the device and one slot, on this Mac |
| Queue round | `queue.lanes` | what its lanes need, and the worker's checkout |

Admission knows a request's kind, its arrival and its label. It does not know who asks: a queue's
gate and a session's hand-run lane are requests alike, and two one-slot requests count the same
whether one uses 3% of a core or every core. Both facts matter under [decisions](#decisions).

## Policies in force

Each rule has the reason it was made and the decision or incident that set it. The cited guides
hold the mechanism.

### On one Mac

| # | Rule | Why | Set by |
| --- | --- | --- | --- |
| 1 | All heavy work asks one queue per Mac. A private pool is a second queue and is not allowed. | Ten unbounded builds panicked a Mac, and a private pool ran the load to 764. | 2026-10-14 |
| 2 | First come, first served, with no class by size or by who asks: a short check waits as a gate does, and a session's lane may hold a guaranteed slot while a queue's gate waits. | A class that goes first starves what it passes, and admission cannot tell how long work will run. | [ve#200], 2026-11-07; [ve#305]; 2026-11-17, [D3](#d3-may-hand-run-lanes-hold-guaranteed-slots-while-a-gate-waits) |
| 3 | A Mac's `busyLimit` bounds every admission but its first. Three guaranteed slots admit under it, and slots beyond them open one at a time under it once the Mac has settled and while its memory pressure is normal. At or over it, a Mac admits one job, and only while nothing admitted runs there. | A fixed three left a Mac idle; no limit overloaded it; the load average misread a Mac running simulators; guaranteed slots that admitted at any reading let three wide lanes hold a Mac over its limit. The first job is kept so that load which never asks cannot stop all work. | [ve#200], 2026-11-06; 2026-11-17, [D4](#d4-does-a-macs-cpu-limit-bound-every-admission) |
| 4 | Each Mac sets its own limits. To load a Mac more, raise `slotsMax` and `busyLimit`, not `slots`. Setup writes a `busyLimit` of 70 for the Mac it runs on and never changes one that is set; the default, which an added Mac keeps, is 90. | Another Mac is to be loaded most and this Mac a little less, by each Mac's setting and by no rule that names a Mac. Each guaranteed slot added makes a Mac worse for gates. | 2026-11-07; 2026-11-17, [D7](#d7-no-mac-is-special) |
| 5 | One whole-host lane per Mac, and nothing else admitted while it runs. | A gate's load dips between phases; work admitted on a dip left a gate unmeasured after 1,116 s. | [ve#196], 2026-11-05 |
| 6 | A whole-host lane that waits keeps the guaranteed slots from later arrivals, which run only beyond them: on this Mac, and through a place in its queue on every other Mac that can run the lane (rule 30). | Sessions' lanes took each slot as it freed, and a gate could wait out its 5,400 s. | [ve#174], 2026-11-05 |
| 7 | An older one-slot request holds back a later whole-host or exclusive request. One-slot requests share with each other. | Arrival order holds across kinds, so large requests do not pass a small one that waits. | [ve#200]'s order; kept 2026-11-17, [D2](#d2-may-a-gate-pass-one-slot-work-queued-ahead-of-it) |
| 8 | Exclusive work takes every slot and the model, one request at a time in arrival order, holds back every later request, and starts once the CPU reads quiet or after 300 s. | A timing is void beside other work, and beside the trailing reading of work that just ended. | [#121], 2026-11-13 |
| 9 | Only a timed part is exclusive. A gate suite never is, and a check that voids itself on a busy Mac does not belong in a shared gate. | An exclusive gate held another Mac for 33 minutes; a self-voiding check held mpm's queue for an afternoon. | 2026-11-12, [#28] |
| 10 | One model or GPU run per Mac, with one slot and not the host. | The hardware runs one; exclusive admission would hold back builds that need not wait. | [model admission][model] |
| 11 | A request keeps its arrival and its place while it is offered to other Macs. A place not renewed for 180 s holds nothing back. | A gate away on an offer lost its turn twice in 45 minutes. | [#160], 2026-11-15 |
| 12 | Nothing is preempted. Admission judges a Mac when work starts and not after. A wait ends at 5,400 s with nothing run. | Stopping admitted work loses it. The cost is [#28]: work admitted later can void a run. | [admission][admission] |
| 13 | An owner that has ended, or cannot be confirmed running, holds nothing. | One stranded entry refused every session's heavy work. | [#23], 2026-11-13 |

### Across the Macs

| # | Rule | Why | Set by |
| --- | --- | --- | --- |
| 14 | Queuekeeper chooses the Mac: of the Macs with room, this Mac among them, the one with the most headroom, which is its CPU limit less its CPU's busy share; the farm's order between equals, this Mac after the Macs it lists; and whichever admits first when none has room. | The Macs are for work. Each Mac's own limit says how much it is to be loaded, so load spreads and the workstation's lower limit makes it the later choice at equal load. "Another Mac first" filled one Mac's slots and sent gates to this one. | [ve#168], 2026-11-04; 2026-11-17, [D5](#d5-how-is-load-balanced-across-the-macs) |
| 15 | Every lane is offered unless it says `here` with a reason: a device, a measurement tied to this Mac's hardware, a self lane's parts. | A lane's first run elsewhere is its check; a difference between Macs is a defect to fix. | 2026-10-28, 2026-11-07 |
| 16 | A Mac has room by its own admission's rules: a free slot, guaranteed or beyond, under that Mac's `busyLimit`; room for one job at any reading while nothing admitted runs there; and none while whole-host or exclusive work runs or is queued ahead there. The test is one for every Mac, the Mac that chooses included. | An offer sent where admission would hold it costs a checkout and a wait. A Mac with a test of its own is a special Mac. | [ve#186], [ve#191], [ve#201]; 2026-11-07; 2026-11-17, [D7](#d7-no-mac-is-special) |
| 17 | Exclusive work sent to another Mac is offered to start at once, and joins that Mac's queue only after 900 s of declined offers. | A queued exclusive request idles the Mac while it waits; the bound keeps it from waiting forever. | [placement][placement] |
| 18 | A Mac that cannot be reached is passed over and the work runs here. One that is set up wrongly runs nothing and stops for the owner. | Busy or away is ordinary; a different Xcode gives results that cannot be compared. | 2026-10-27, 2026-10-28 |
| 19 | A lane that needs an unlocked screen goes only to a Mac whose screen reads unlocked, and waits holding no slot, and no place in the queue of a Mac whose screen is locked (rule 30). A locked screen holds back only work that declares the need, and only on its own Mac: a bisect's older half is offered to every Mac as any gate is (rule 24). | A gate sent to a locked Mac ran unmeasured three times; an older half waited 67 minutes on this Mac's lock while another Mac stood free and unlocked. | [ve#185], [ve#293]; [#205], 2026-11-17 |
| 20 | One holder of a Mac's foreground, only on Macs marked for it, taken before admission. | The screen is the owner's. A waiter holds no slot, and builds go on beside a foreground run. | [foreground][foreground] |
| 21 | Devices and pool simulators have one holder each, in turn, under a reservation. The owner's phone needs his word first. | A device is one thing, and some are the owner's own. | 2026-11-06, [devices][devices] |
| 30 | A whole-host lane that waits takes a place in the queue of every Mac that can run it, runs on the first to admit it and gives up the others. A place holds no slot and no checkout, is renewed by the Mac that sent it and lapses within 180 s of that Mac's silence. One process starts the lane, in one place at a time, so it never runs twice. A lane that needs an unlocked screen has its place on a Mac only while that Mac's screen reads unlocked. | Gates queued for one Mac while another ran none for 89% of the day: nothing drained toward a gate that was only offered there. A place on a locked Mac would keep its slots for a lane it cannot run. | 2026-11-17, [D6](#d6-gates-wait-mostly-for-other-gates); [#214] |

### In a merge queue

| # | Rule | Why | Set by |
| --- | --- | --- | --- |
| 22 | One worker per repository takes everything waiting, oldest first, as one batch with one gate. | A gate's time is paid once for the batch. | [the merge queue][queue] |
| 23 | A failed batch gets the cheap explanations first: failed tests alone, then one entry the failure points at, then a bisect. | A bisect costs about a gate for each halving. | [when a batch fails][fails] |
| 24 | A bisect's halves gate at once on two different Macs: the older is offered as any gate is, and the newer goes to any Mac but the one that admitted the older, if one can start it now. | Each halving then costs one gate's time, not two. Which Mac runs which half is placement's choice, as for any lane. | [ve#241], 2026-11-07; 2026-11-17, [D7](#d7-no-mac-is-special) |
| 25 | Entries that arrive during a gate are gated on top of its tip, on any Mac but the one that admitted the gate, offered as any gate is, one such gate at a time. | The next round's gate is under way before this one lands. | [ve#241], 2026-11-07; 2026-11-17, [D7](#d7-no-mac-is-special) |
| 26 | A docs-only entry never rides a code gate. It lands first or beside the gate, and passes an older entry only when the two change no path in common. | Documents need no code gate, and the order of entries that share a path never changes. | [ve#394], 2026-11-11 |
| 27 | The worker runs preflight lanes and the docs check without admission. One that does not end in a minute runs again admitted. | Two checks of a few seconds each waited 43 minutes for slots. | [#169], 2026-11-15 |
| 28 | A run with no verdict blames nobody and runs again once the Mac has settled. Three in a row hold the queue. A run a locked screen voided does not count, up to five. | A cause in the Mac says nothing about the commits; a cause that does not clear needs a person. | [ve#293], 2026-11-08; open as [#24] |
| 29 | `--front` and `--no-gate` are for emergencies, with the reason recorded. | A fix every queued entry needs should not wait behind them. | [ve#163], 2026-11-04 |

Rules 26, 27 and 29 are the exceptions to rule 2. Each lets short work go first where it takes no
slot or changes no order that matters, and none is a class of work with priority in admission.

## Decisions

Each is the owner's, made on 2026-11-17 against the goals above. A decision that changes behavior
is not in force until it is built, and [Policies in force](#policies-in-force) changes when it is.

| | Decision | Outcome | In force |
| --- | --- | --- | --- |
| D1 | The order of goals | Confirmed | Yes |
| D2 | A gate passing one-slot work queued ahead | No: arrival order stays | Yes, unchanged |
| D3 | Hand-run lanes holding guaranteed slots | Yes: the rule stays | Yes, unchanged |
| D4 | A Mac's CPU limit bounding every admission | Yes, but the first on a Mac with nothing admitted | Yes (rules 3 and 16) |
| D5 | Balancing across the Macs | By headroom against each Mac's own limit | Yes (rule 14) |
| D6 | Gates waiting for gates | A waiting gate takes a place in each Mac's queue | Yes (rule 30) |
| D7 | No Mac is special | Setup writes 70% for its Mac; an added Mac's default is 90% | Setup's limits, the one test of room, this Mac as a candidate like any other and the queue's pairs: yes (rules 4, 14, 16, 24 and 25). The foreground rule that names this Mac, and the settings for kinds, labels and devices: not built, [#211] |

### D1: Is the order of goals right?

**Confirmed, 2026-11-17.** One consequence is accepted with it: with limits (2) above bounded waits
(3), a Mac kept over its limit by work that never asks admission delays everything but the one job
D4 always allows, and a request there can end its 5,400 s wait with nothing run.

Everything below leans on the order. It puts a bounded wait (3) above throughput (4), which is how
[ve#174] and [ve#200] were decided, and puts each Mac's limits (2) above both, which the rules in
force did not do until [D4](#d4-does-a-macs-cpu-limit-bound-every-admission) was built. [#84] records the
owner's view of 2026-11-11 that slots and the heavy and light kinds should go, with work admitted
by each Mac's measured headroom. That is a change of mechanism under the same goals, and D3 to D5
are steps toward it or away from it.

### The measurement behind D2 and D3

[#171] asks both questions. Its [latest comment][171-split] replays six whole-host gates' waits on
this Mac for 2026-11-15, second by second, 14 h 34 min in all:

| What the gate waited for | Time | Share | Decision |
| --- | --- | --- | --- |
| Another whole-host lane holding the Mac | 5 h 39 min | 39% | D6 |
| Another whole-host lane queued ahead | 4 h 08 min | 28% | D6 |
| Guaranteed slots busy, with nothing queued ahead | 3 h 21 min | 23% | D3 |
| One-slot work queued ahead | 1 h 10 min | 8% | D2 |
| Exclusive work | 15 min | 2% | |

The replay has three limits. It is one day on one Mac. It counts only the seconds a gate waited
here, not its offers to another Mac. And "guaranteed slots busy" includes one-slot work that was
queued ahead of the gate, was then admitted, and went on holding a slot, so part of the 23% is rule
7's cost and not rule 3's. The replay does not separate the two.

A second reading of the same log, made for this guide, asks how busy this Mac was while those gates
waited for slots with no whole-host lane holding it. Each wait the log records is a window of up to
two minutes, with the CPU's busy share at its end and the highest during it, set here against this
Mac's limit of 70%:

| While a gate waited for slots | Time | Share of all waits |
| --- | --- | --- |
| The CPU read at or over the limit | 4 h 22 min | 30% |
| The CPU read under the limit only at the window's end | 34 min | 4% |
| The CPU read under the limit for the whole window | 19 min | 2% |

So for most of the time that slots kept a gate out, the Mac was as busy as its limit allows, whoever
held the slots. That day a whole-host lane held this Mac for 40% of the 24 hours and another Mac
for 11%, by `qk admissions usage`, while another Mac ran three or more jobs 48% of the time.

### D2: May a gate pass one-slot work queued ahead of it?

**Decided, 2026-11-17 ([#171]): no. Arrival order stays (option A).** What was weighed is kept
below.

*The rule (rule 7).* A one-slot request at the head of the line waits when every guaranteed slot is
held and none beyond them may open. A whole-host gate queued behind it waits for it. The gate
already keeps the guaranteed slots from work that arrives after it (rule 6), so the only work it
yields to is work older than itself. When a slot frees, the older request takes it, and the gate
then also waits for that work to end.

| Option | Gains | Costs |
| --- | --- | --- |
| A. Keep arrival order. **Chosen.** | No request is ever passed by a later one. | The 8% above, and the unknown part of the 23%. |
| B. The gate keeps the guaranteed slots from older one-slot work too, which runs only beyond them. | At most that 8% and that part. | Older work is passed by a gate, and then by each gate behind it. Nothing is admitted beside a running gate (rule 5), and gates ran or queued back to back for two thirds of the measured wait, so one-slot work on that Mac could wait for the whole chain. |
| C. As B, for a bounded time or one gate, after which the older request holds its place as today. | Most of B's gain. | A second timer, as rule 17 has, and a wait that is harder to explain in one line. |

A was chosen because goal 3 outranks goal 4 and the measured gain is small. Should B be wanted
later, C's bound is what keeps goal 3.

### D3: May hand-run lanes hold guaranteed slots while a gate waits?

**Decided, 2026-11-17 ([#171]): yes. The rule stays (option A).** What was weighed is kept below.

*The rule.* Admission does not know who asks. Once a gate is queued, later arrivals cannot take a
guaranteed slot (rule 6). The slots that keep it waiting are held by work admitted before it
arrived: sessions' lanes of 25 minutes to two hours on 2026-11-15, and in [#84] one device run at
3% CPU that kept a gate waiting more than 39 minutes at a load of 4.6.

| Option | Gains | Costs |
| --- | --- | --- |
| A. Keep. **Chosen.** | Nothing changes; a hand-run lane is admitted as any work is. | Up to the 23% above, most of it time the Mac was at its CPU limit. |
| B. Hand-run work runs only beyond the guaranteed slots, which are kept for queues' gates. | The gate finds its slots free more often. | Admission must learn who asks, which makes the first priority class since [ve#200] removed them. Hand-run work then starts only under `busyLimit`, and on 2026-11-17 both Macs read over theirs: goal 5 could become starvation, against goal 3. The guaranteed slots idle when no gate waits. |
| C. A gate stands in: it takes the free guaranteed slots, and slots beyond them for the ones that are held, while the Mac's CPU reads under its `busyLimit` and no other whole-host or exclusive work runs. | A light holder, such as [#84]'s, no longer keeps a gate out, and nobody loses a place. The Mac's own CPU limit decides, as the owner asked on 2026-11-17. | The gate runs beside the holders, so its time is no longer the Mac's alone ([ve#196]), and a busy holder still keeps it out, as it should. |
| D. Only long or device-bound work runs beyond the guaranteed slots. | Narrower than B. | Work must declare its length, and B's cost applies to what does. |

C is [#84]'s first interim change, and this guide first suggested it. The second table under
[the measurement](#the-measurement-behind-d2-and-d3) changed that. On 2026-11-15, C would have
started a gate sooner during 19 to 53 minutes of 14 h 34 min, 2% to 6%, because the Mac read at or
over its limit for the rest of the time slots kept a gate out. B would have started gates sooner
only by running them on a Mac already at its limit, against goal 2. So A was chosen: on this
evidence the slots' holders were using the Mac, and the wait is a shortage of Macs for gates, which
is D6. C stays a small, safe change for a light holder such as [#84]'s, worth making only if such
holders prove common on other days.

### D4: Does a Mac's CPU limit bound every admission?

**Decided, 2026-11-17: yes, but the first (option B).** A Mac over its limit takes no more work,
except one job where nothing admitted runs. In force as rules 3 and 16, built after D5 as the
decision required.

The owner's direction is that scheduling takes each host's CPU limit into account.
[Resources](#resources) lists what the limit does and does not bound today. One cost of the gap is
recorded: three admitted lanes drove another Mac's load into the hundreds, and Queuekeeper's own
gate ran to its 2,400 s limit twice ([#170]). Another is not measured: [#12] asks whether a
whole-host gate should start on a Mac whose CPU reads over its limit, and the readings there proved
to be mostly the gate's own load.

| Option | Gains | Costs |
| --- | --- | --- |
| A. Keep: the limit throttles only slots beyond the guaranteed. | Three requests always start, so no reading can starve them. | A Mac runs over its limit whenever three wide lanes hold its slots. |
| B. The limit bounds every admission but the first: a guaranteed slot opens over the limit only while nothing admitted runs there. **Chosen.** | The limit means what its name says. An over-limit Mac takes no more, and placement sends the work to a Mac with room. | It reverses 2026-11-07's rule that a free guaranteed slot is room at any reading, made when another Mac declined work at 93% against 90% and this Mac took it. Load that never asks, such as the owner's own work, can then delay the second admission; the first is kept so that it can never stop all work. |
| C. Count a lane's width: a lane that starts many processes takes more than one slot. | Slots mean something again. | Each lane must declare or be measured for its width. |
| D. Admit by headroom alone, with no slots ([#84]). | One measure for every admission. | The largest change, and with no guaranteed slot the bound of goal 3 has to be rebuilt in the queue's order. |

B was chosen as the smallest change that makes the limit binding, and it is safe only together
with D5: without balance, an over-limit Mac that refuses work pushes it all to this Mac.

### D5: How is load balanced across the Macs?

**Decided, 2026-11-17: by headroom against each Mac's own limit (option B).** It took the place of
"another Mac first" and is in force as rule 14, built as step 3 of [#211].

The owner's direction is to balance where possible. Rule 14 as it stood ordered the Macs; it did
not balance them. One-slot work filled another Mac's guaranteed slots first. A gate, which needs
all of them free, then seldom found room there and ran on this Mac. On 2026-11-06 another Mac was 97% busy
with three or four jobs for 48% of three hours while this Mac ran one gate for 84% of them. On the
morning of 2026-11-17, over three hours, 40 of this Mac's 47 admissions had first been offered to
another Mac and declined there.

| Option | Gains | Costs |
| --- | --- | --- |
| A. Keep the farm's order. | This Mac stays free whenever another has room, which was the reason for "another Mac first". | The Macs fill unevenly, and gates gather on this Mac. |
| B. Among the Macs that can run the work, choose the one with the most headroom against its own limit, and use the farm's order for ties. **Chosen.** | Load spreads, and each Mac's limit carries the owner's preference: this Mac's lower limit makes it the last choice at equal load. | A reading is a 15 s window and a gate's load dips, so several placements can choose the same quiet Mac. Leaving a warm checkout costs a colder build. |
| C. Balance among the other Macs only, with this Mac last as today. | Keeps this Mac for the owner. | With one other Mac it changes nothing, and the farm has run with more than one only in tests ([#207]). |

"Where possible" has fixed edges that no option moves. A lane that says `here`, a device, a marked
foreground, an unlocked screen and a lane's `chips` each name the Macs that can run it, and balance
chooses only among those.

### D6: Gates wait mostly for other gates

**Decided, 2026-11-17: a waiting gate takes a place in each Mac's queue (option B).** In force as
rule 30, built as step 4 of [#211]. A gate that needs an unlocked screen has its place on a Mac
only while that Mac's screen reads unlocked, and gives it up when the screen locks: a place there
would keep a Mac's slots for a lane its locked screen cannot run ([#214]).

Two thirds of the measured wait was one whole-host gate behind another, which neither D2 nor D3
touches. Rule 5 allows one per Mac, and six projects' whole-host gates shared two Macs. In effect
they shared little more than one: on 2026-11-15 a whole-host lane held this Mac for 40% of the day
and another Mac for 11%. A gate waits in this Mac's queue, where rule 6 drains the guaranteed slots
toward it. Another Mac is only offered the gate, to start at once or not at all, so nothing drains
there, and rule 14 as it then stood sent one-slot work there first, which kept its guaranteed slots held.

| Option | Gains | Costs |
| --- | --- | --- |
| A. Keep. | This Mac is the gates' Mac and another Mac the one-slot work's, which always has somewhere to run. | Gates queue for one Mac while another ran none for 89% of that day. |
| B. A gate that waits takes a place in each Mac's queue that can run it, runs on the first to admit it, and gives up the others. **Chosen.** | Rule 6 then drains every Mac toward a waiting gate, so every Mac takes gates and not one, with no new hardware and no new kind of priority. It is what rule 17 does for exclusive work after 900 s. | One-slot work that arrives while gates wait runs only beyond the guaranteed slots on every Mac, so under each Mac's CPU limit. Work older than the gate still goes first (D2). |
| C. More Macs ([cloud gate runners](cloud-gate-runners.md)). | Capacity that no rule can add. | Money, and a farm with more than one other Mac has run only in tests ([#207]). |
| D. Two gates on one Mac while its CPU allows. | Uses a Mac's idle cores between a gate's phases. | Undoes rule 5: on 2026-11-04 two gates on a 10-core Mac ran at a load of 719, and 8 of 12 gates there that day gave no verdict. |
| E. Gates that bound their own width and take one slot. | Several run on one Mac. | Honest only if a slot counts a lane's width (D4's option C); otherwise it is [#170]. |

B was chosen first because it costs nothing but code and its effect can be read the next day, as
each Mac's share of time held by a whole-host lane in `qk admissions usage`. C follows only if gates
still queue once both Macs take them. Not measured: how many of the day's gate runs were reruns
after a run with no verdict, each of which holds a Mac again ([#170], [#24]).

### D7: No Mac is special

The owner's direction is that no admission or scheduling decision is written for a special primary
Mac. Setup is the one exception, by design: it takes the Mac it runs on to be the owner's and gives
it a lower CPU limit than an added Mac's default: 70% for the Mac setup runs on, and 90% as the
default for a Mac that is added. That part is in force (decided 2026-11-17; rule 4): the setup
script writes the 70 into its Mac's limits file unless the file sets a CPU limit already, and the
code's default is 90. Until then the default was 75% for every Mac and no command wrote a limits
file. Building the rest is [#211], which holds the code references. What it changes:

| Today's rule that names this Mac | In its place |
| --- | --- |
| Other Macs are offered work first, and this Mac is the fallback; it is not in the farm's list (rule 14). | This Mac is a member of the list, and the choice is D5's. In force: this Mac is a candidate for every offer it may take, and `farm.json` still lists only the others, so it comes after them between equals. |
| A request waits in this Mac's queue, and other Macs get only offers to start at once (rules 6 and 17). | A place in the queue of each Mac that can run it (D6, decided). In force for a whole-host lane (rule 30); one-slot and exclusive work wait as before. |
| A bisect's older half gates here and its newer half never here; a stacked gate is placed by where the batch's last lane ran (rules 24 and 25). | Any two different Macs with room. In force: the first gate of a pair is offered as any gate is, and the second goes to any Mac but the one that admitted it, this Mac among them. What is left follows from where a command starts: a request waits in the queue of the Mac its process runs on, so a second gate kept off this Mac starts on another Mac at once or not at all. |
| Foreground work never comes to this Mac by itself, and goes to the first listed Mac when none is marked (rule 20). | The foreground mark alone, on any Mac. |
| Another Mac's slots beyond the guaranteed count as room when the farm has several Macs; this Mac's do not (rule 16). | One test of room for every Mac. In force. |
| Every Mac has the same default limits, and no command writes a limits file. | Setup writes this Mac's file, with its lower `busyLimit`. In force. |

One setting is missing: nothing says which kinds of lane a Mac may run, apart from the foreground
mark and a lane's `chips`. With it, keeping gates off the owner's workstation is his setting and
not a rule. What stays is what follows from where a command starts: work that names this checkout's
ignored files, a local path, a pool simulator or a device attached here runs here, and results come
back to the Mac that sent the work.

**The setting, decided 2026-11-17: kinds plus labels.** A Mac says which kinds of work it takes
(whole-host gates, exclusive work, model runs, foreground), which is the owner's preference, and
which labels it has, such as a license; a lane says which labels it needs. A lane is not named in
a Mac's settings. A device is the case labels do not fit. An iPad or iPhone paired over Wi-Fi is
reachable from every Mac, and one kept to a USB cable from one Mac only, so it belongs to no Mac.

**A device is declared, and its reach is read (decided 2026-11-17).** A device is a resource of the
farm with one reservation record. The owner declares each device: the ways a Mac may reach it, in
his order of preference, such as a cable first and then the network, and what it means when no Mac
reaches it, an error or a device that is away. Each Mac reports which declared devices it reaches
now and how. Placement chooses among the declared ways only, and what was read never adds a device
to the farm or takes one out: a device that must always be reachable and is not is reported as an
error, and one that may be away is offline and no error. The settings' names are for [#211].

### Smaller questions with issues of their own

- **Work admitted later can void a run** (rule 12, [#28]). The owner's expectation of 2026-11-12 is
  that an entry waits for capacity and then runs to a verdict. Timed parts now go exclusive (rule
  9). Whether a queue's gate should outrank sessions' runs was D3: it does not.
- **A hold after three runs with no verdict** (rule 28, [#24]). The owner's objection of 2026-11-12
  is that a cause which clears by itself should alert, not stop a queue. That is goal 4 against the
  need for a person where retrying cannot help.
- **What the log can say.** The admission log records the same refusal kind for a gate behind
  one-slot work and a gate short of free slots, which is why the table above took a replay
  ([#171]). A kind for each would let `qk admissions` report this split every day.

## Weighing a change

A proposal to change a rule says which goal it serves, which it costs, and what bounds the cost. It
then shows the reading that would prove it wrong. Two readings exist today: `qk admissions usage`
for each Mac's capacity, limits and share of time at each count of admitted jobs, and each Mac's
admission log for every wait, with its reason and the load and busy share it saw
([admission log][log]). Once the owner decides, the decision goes into the table above with its
date, and the mechanism's guide says how it works.

[admission]: shared-host.md#heavy-work-admission
[placement]: shared-host.md#which-mac-runs-a-lane
[farm]: shared-host.md#the-farms-macs
[model]: shared-host.md#model-admission
[screen]: shared-host.md#screen-state
[unseen]: shared-host.md#load-the-queue-cannot-see
[log]: shared-host.md#admission-log
[devices]: shared-host.md#device-reservations
[foreground]: foreground.md#waiting-for-the-foreground
[queue]: maintenance.md#the-merge-queue
[fails]: ../merge_queue.md#when-a-batch-fails
[171-split]: https://github.com/vocaro/queuekeeper/issues/171#issuecomment-6954024828
[#12]: https://github.com/vocaro/queuekeeper/issues/12
[#23]: https://github.com/vocaro/queuekeeper/issues/23
[#24]: https://github.com/vocaro/queuekeeper/issues/24
[#28]: https://github.com/vocaro/queuekeeper/issues/28
[#84]: https://github.com/vocaro/queuekeeper/issues/84
[#121]: https://github.com/vocaro/queuekeeper/issues/121
[#160]: https://github.com/vocaro/queuekeeper/issues/160
[#169]: https://github.com/vocaro/queuekeeper/issues/169
[#170]: https://github.com/vocaro/queuekeeper/issues/170
[#171]: https://github.com/vocaro/queuekeeper/issues/171
[#205]: https://github.com/vocaro/queuekeeper/issues/205
[#207]: https://github.com/vocaro/queuekeeper/issues/207
[#211]: https://github.com/vocaro/queuekeeper/issues/211
[#214]: https://github.com/vocaro/queuekeeper/issues/214
[ve#163]: https://github.com/vocaro/vocaro-engineering/issues/163
[ve#168]: https://github.com/vocaro/vocaro-engineering/issues/168
[ve#174]: https://github.com/vocaro/vocaro-engineering/issues/174
[ve#185]: https://github.com/vocaro/vocaro-engineering/issues/185
[ve#186]: https://github.com/vocaro/vocaro-engineering/issues/186
[ve#191]: https://github.com/vocaro/vocaro-engineering/issues/191
[ve#196]: https://github.com/vocaro/vocaro-engineering/issues/196
[ve#200]: https://github.com/vocaro/vocaro-engineering/issues/200
[ve#201]: https://github.com/vocaro/vocaro-engineering/issues/201
[ve#241]: https://github.com/vocaro/vocaro-engineering/issues/241
[ve#293]: https://github.com/vocaro/vocaro-engineering/issues/293
[ve#305]: https://github.com/vocaro/vocaro-engineering/issues/305
[ve#394]: https://github.com/vocaro/vocaro-engineering/issues/394
