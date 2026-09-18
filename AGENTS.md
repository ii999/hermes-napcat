# Project conventions

Use Python 3.12+, the src layout and uv. Keep protocol and transport modules independent of Hermes; import Hermes only at the gateway boundary. Never patch Hermes core from an installer.

README.md and docs/ define the implementation contract. Update the capability matrix when adding or removing behavior. Mark simulated, source-inspected and real-environment validation separately. Do not claim a real QQ or Hermes integration test from a fake interface fixture.

Keep tests focused on protocol behavior, authorization, delivery ambiguity and resource limits. Do not add tests that freeze incidental names, wording or formatting. Run `uv run pytest -q`, `uv run ruff check .` and `uv build` when dependencies are available; report unavailable checks rather than suppressing errors.

Secrets belong in the operator's profile environment. Keep profile-scoped reads, deny-by-default authorization, target ACL checks and SSRF protections. Do not expose arbitrary OneBot actions to the model. Never retry a side-effecting action after an uncertain acknowledgment.

Use subprocess argument arrays with shell=False. Work only inside a dedicated project directory. Never use a filesystem root, drive root, home directory or user profile directory as cwd. Do not nest shells. Never delete arbitrary user files, follow incoming local file paths or commit actual account credentials.
