# ABICheck Bazel Lab

A small public C++ shared-library project used to validate ABICheck GitHub Actions.

- `baseline.yml` builds `//:math`, collects a source-aware ABI snapshot, and commits it to `abi/math.abicheck.json` on `main`.
- `abi-scan.yml` runs on pull requests, builds the candidate with Bazel, then executes an ABICheck source scan against the committed baseline.

Test PRs intentionally exercise compatible additions, binary ABI breaks, source/API breaks, and implementation-only changes.
