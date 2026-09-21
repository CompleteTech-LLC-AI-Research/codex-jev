# Evidence: the host produces the duplicate pair itself, and the boundary projects it

Closes [#78](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/78).
Answers the third acceptance item of
[#5](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/5) in the
form that item is usually read as meaning: **a real Codex run demonstrates
reduced outgoing content and an exact reset** on a body the *host* produced
during the turn, not on a transcript a fixture handed it.

Companion to [`projection-real-host.md`](projection-real-host.md), which proves
the same boundary on a seeded rollout, and to the boundary description in
[`BUS_BOUNDARY.md`](../BUS_BOUNDARY.md). The two are deliberately different
claims, not two copies of one:

| | `projection-real-host.md` | this document |
| --- | --- | --- |
| Where the eligible pair comes from | hand-written into a rollout file | the host executes a read tool and answers itself |
| Host entry point | `codex exec resume` on a seeded session | `codex exec` + `codex exec resume` |
| Cases | `pristine` / `off` / `on` | `off` / `none` / `on` |

Nothing here is a statement about model accuracy, safety, or performance. It
records what was executed, on which revision, and what was not executed.

## Revisions

| Item | Value |
| --- | --- |
| Verified revision of this repository | `044a01dd1e503024438dc8ea2ce313f2c06d18a0` (`main`) |
| Binary exercised | `/home/agent/jev/work/lead/target/debug/codex`, sha256 `8da2941611b27c2963820573508d31170f8f2f8738476b17b5cce73e2101d1d6` |
| Toolchain | `rustc 1.95.0 (59807616e 2026-04-14)`, `cargo 1.95.0 (f2d3ce0bd 2026-03-21)` |
| Pinned component | `jev-prune-kit` at `2ecc8ff4e0976991c7abc09287d8f2f736d3164c` |
| Adapter under test | [`jev/scripts/bus_boundary.py`](../scripts/bus_boundary.py) (`apply`, resolved by the host) |
| Tier | `real-host-binary` |

The binary is the **same debug artifact** the companion document cites, so the
two runs are directly comparable. It is a debug build, not the release
artifact.

## Why there is a read tool at all

`projection-real-host.md` records that this revision exposes no read-family tool
under its default configuration, so a duplicate pair cannot arise during a
turn. That is true by default and false with the memories feature on. Under

```toml
[features]
memories = true

[memories]
use_memories = true
dedicated_tools = true
```

the host's own outgoing request announces a `namespace` tool group named
`memories`:

```json
{"type": "namespace", "name": "memories", "description": "Tools in the memories namespace.",
 "tools": [{"type": "function", "name": "read",
            "description": "Read a Codex memory file by relative path, ..."}]}
```

Its members are `add_ad_hoc_note`, `list`, `read`, `search`. The call the
transcript makes is a `function_call` carrying `"name": "read"` and
`"namespace": "memories"`, and the pinned policy's `READ_TOOLS` matches the bare
name — the same pair the boundary's own carrier proves and the router routes.

The host then **executes** the call. The recorded result is the host's own
envelope around the file's bytes, not a string the fixture wrote:

```json
{"path": "jev-probe.txt", "start_line_number": 1,
 "content": "line 001: deterministic memory probe content alphabet soup\n...",
 "truncated": false}
```

That is the whole point of this fixture: the eligible body is produced inside
the turn by the host's tool path.

## What the run executes

`jev/smoke/run-read-tool-projection-smoke.sh` starts a **real Codex host
process** against a **loopback Responses-API mock**
([`jev/smoke/mock_responses_server.py`](../smoke/mock_responses_server.py),
`--script projection`) inside an isolated `CODEX_HOME`. No provider is
contacted and no credential is read: the only reachable endpoint is the mock,
and the generated config declares a local `model_provider` with
`request_max_retries = 0`.

The scripted turn answers with a `memories` `read` call for each of the first
eleven turns. The first two name the same path with identical arguments, so the
host answers both with byte-identical bodies; the remaining nine are distinct.
A second `codex exec resume` turn then resends the whole transcript, which is
the request in which the pair has been pushed out of the current user turn *and*
out of the `RECENT = 16` tail the policy protects.

All three cases share one `CODEX_HOME` path and one workspace, so the only
difference between their recorded request bodies is the boundary itself. A
differing scratch path would leak into the transcript through the skills and
environment preambles; the fixture therefore compares its three cases inside a
single scratch directory and snapshots the rollout store between them.

## Results

Affected turn (the resumed second user turn):

| Case | `JEV_*` environment | Serialized `input` bytes | Input items | Omission markers |
| --- | --- | --- | --- | --- |
| `off` | boundary configured, `JEV_SWITCH_PROJECTION_DEDUP_RECEIPTS=0` | 73501 | 27 | 0 |
| `none` | no adapter at all | 73501 | 27 | 0 |
| `on` | same, switch `=1` | 71064 | 27 | 1 |

`check_read_tool_projection.py` asserts, and this run satisfies:

| # | Assertion | Observed |
| --- | --- | --- |
| 1 | The switch on shrinks the affected turn | `71064 < 73501`, **2437 bytes fewer** |
| 2 | Neither the item count nor the tool-result count changes | `27 / 27 / 27`, 11 results in each |
| 3 | The switch off is an exact reset | `input` byte-identical to the unswitched control once per-run ids are masked |
| 4 | The switch off writes no marker | `0 / 0` markers in `off` and `none` |
| 5 | Exactly one body is projected when on | `1`, on `call_jev_projection_01` only |
| 6 | The marker matches the pinned shape | `[Jev prune: repeated read-result body omitted; retained witness: call_jev_projection_02]` |
| 7 | The witness exists, is not the source, and still holds a full read body | witness present, `call_id != witness`, body ≥ 256 bytes |
| 8 | No case persists a projected body into its rollout | `on` and `off` rollouts: marker `0`, full body retained |
| 9 | A standalone adapter re-run resets exactly | `exact reset True` |
| 10 | A receipt is emitted only when switched on | receipts `1` on / `0` off, nothing applied with the switch off |

The absolute totals move with the scratch path length, because that path leaks
into the transcript preamble; the **reduction does not**. Three runs into
different scratch directories produced `73528 / 71091`, `73501 / 71064` and
`73504 / 71067` — 2437 bytes saved in every one, `27` items, one marker, an
exact reset each time. That is why the checker asserts a reduction and an exact
reset *between the three cases of one run* rather than against a constant.

## What this does not show

- **No live provider and no token metric.** The *model's choice* to call the
  tool is scripted by the mock; only the tool's execution is the host's. The
  reduction is serialized request bytes offered to a loopback mock. No token
  count is claimed here or anywhere else in `jev/`. Live-host validation is
  [#25](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/25).
- **Bytes only, never items.** The item count is unchanged in every case,
  because the merged host refuses a changed count (#55) and passes no approved
  view to the carrier. Reconciling the two rules is
  [#58](https://github.com/CompleteTech-LLC-AI-Research/codex-jev/issues/58).
- **The tool is not a default.** Everything above needs `[features] memories`
  and `[memories] dedicated_tools`. Under the default configuration the
  companion document's limitation stands unchanged.
- **The stage is the stub.** The projection is performed by
  `jev/tests/bus_stage_stub/dedup.py`, the pinned stand-in for the
  `jev-prune-kit` stage, not by the real component.
- **One debug build on one platform.** The sha above is this machine's debug
  binary; no release artifact was rebuilt for this evidence.

## Reproduce

```sh
cd /home/agent/jev/work/lead/codex-jev
cargo build -p codex-cli --bin codex        # in codex-rs/, with the target dir and
                                            # OPENSSL_* environment recorded in ISOLATED_ENV.md
./jev/smoke/run-read-tool-projection-smoke.sh \
  --workdir /home/agent/jev/work/lead/verify/readtool \
  --codex /home/agent/jev/work/lead/target/debug/codex --keep --timeout 240

# the fixture's own negative controls; no Codex binary is needed
python3 jev/smoke/self_test_read_tool_projection.py
```

Exit codes: `0` assertions passed, `1` an assertion failed, `2` usage error. A
failed run keeps its scratch directory so the recorded bodies can be inspected.
