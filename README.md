# Blast Radius entry: test selection from a recorded map of sqlglot

For every patch, run only the tests that can observe the change, and split them
over as many workers as pay for themselves (busiest worker + 2 s per worker).

## How it decides

Offline, once, on the pinned sqlglot commit (`eb11aec`), `tools/rebuild_map.sh` runs
sqlglot's whole test suite and records, per test:

* **line coverage** (which source lines it executes),
* **files it opens** (SQL fixtures, CSVs),
* **classes it instantiates**, with their base classes (dialect, parser, generator,
  tokenizer and expression classes).

Everything is stored in `map.json.gz` (about 1.5 MB). Nothing in it is keyed by patch or by
kind of edit. It is only facts about the pinned commit, so it is the "maps built from the
pinned commit" the rules allow. `sources.zip` holds that commit's Python files so the
selector can apply a patch in memory without a network.

At run time `selector.py` reads each patch and picks tests like this:

| change | tests chosen |
| --- | --- |
| a line inside a function | tests that executed the statement holding that line |
| a statement added between others | tests that ran the neighbouring statements |
| class- or module-level data (a dict, a constant, a base class) | tests that used that class or a subclass, plus tests that ran the file |
| a SQL fixture or other data file | tests that opened that file |
| comment or blank line only | none (verified by tokenising old and new code) |
| a docstring | the doctest tests |
| a syntax error | tests that ran any line of the file |
| `conftest.py`, packaging or pytest config | everything |
| a patch that does not apply cleanly, a rename, anything unexpected | everything |

Chosen tests are balanced across workers (longest first) and the worker count with the lowest
simulated cost is used.

Two details that matter for fidelity: `tests/test_optimizer.py` and `tests/test_executor.py`
run their work in process pools, so while tracing, the pools are replaced by in-process
thread pools that still pickle arguments and results (that is what makes `sqlglot.serde`
run). `test_lazy_load` imports sqlglot in a subprocess, so it is given the lines a bare
`import sqlglot` executes.

## Files

* `Dockerfile`, `selector.py`, `map.json.gz`, `sources.zip`: the entry (no network at build or run time).
* `tools/rebuild_map.sh`: rebuilds the map and sources from the pinned commit (needs the
  starter kit's `sample/tests.json` and `requirements.lock`).
* `tools/mutate.py`, `tools/run_mutants.py`, `tools/compare.py`: a mutation harness used to
  check the selector never misses a failing test: it makes random single-line mutants,
  finds their true failing tests by running the suite, and compares them with the selection.

sqlglot is MIT licensed; `sources.zip` contains unmodified files from it.
