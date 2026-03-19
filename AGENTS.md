# Engine Interop Tester AGENTS

## Repo Role

- Internal conformance and interoperability harness for engine-facing APIs.
- Consume shared contracts instead of re-encoding route knowledge wherever possible.

## Internal API Standard

- Current conformance state: consumer-only interop harness with some existing manual route knowledge still `Guarded Legacy`; new interop harness work must be `Contract First`.
- Prefer generated clients and contract-backed assertions over handwritten endpoint duplication.
- If this repo introduces or updates a shared test-facing API contract, publish it under the standard internal API layout and identify the owning source repo.
- Keep this repo out of `clients/*` concerns and do not treat it as a public API definition source.
- Do not embed broker or app-host ownership here. This repo validates the shared broker/app-host surfaces as a client only.
