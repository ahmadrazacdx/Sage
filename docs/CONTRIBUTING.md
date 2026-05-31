# Contributing

> Development workflow and contribution guidelines for the Sage project.

## Development Setup

See [SETUP.md](SETUP.md) for complete environment configuration.

```bash
git clone https://github.com/ahmadrazacdx/Sage.git
cd Sage
uv sync --all-extras
```

## Branching Strategy

| Branch | Purpose |
| --- | --- |
| `main` | Stable release branch. Protected. |
| `dev-main` | Active development integration branch |
| `dev-<name>` | Feature/fix branches, created from `dev-main` |

All work is merged into `dev-main` via pull request. Releases are created by merging `dev-main` into `main` and tagging with a semver version.

## Commit Conventions

This project uses [Conventional Commits](https://www.conventionalcommits.org/).

### Format

```text
<type>(<scope>): <description>
```

### Types

| Type | Usage |
| --- | --- |
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation changes |
| `style` | Code style (formatting, no logic change) |
| `refactor` | Restructuring without behavior change |
| `perf` | Performance improvement |
| `test` | Adding or modifying tests |
| `ci` | CI/CD pipeline changes |
| `chore` | Build scripts, dependencies, maintenance |

### Examples

```text
feat(quiz): add adaptive difficulty scaling
fix(research): handle timeout in digest phase
docs(setup): add CUDA binary setup instructions
refactor(llm): extract port allocation to utility function
test(agents): add diagram validation edge cases
ci(release): add R2 upload step
```

## Code Style

### Python

| Tool | Configuration | Purpose |
| --- | --- | --- |
| Ruff | `pyproject.toml` `[tool.ruff]` | Linting and formatting |
| Mypy | `pyproject.toml` `[tool.mypy]` | Static type checking (strict mode) |

- Line length: 120 characters
- Type annotations required on all function signatures
- Import order managed by Ruff isort rules (`sage` as first-party)

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/
```

### TypeScript (Frontend)

- ESLint and Prettier configuration in the frontend workspace
- Strict TypeScript mode enabled

### Pre-commit Hooks

The project uses pre-commit hooks to enforce code quality before each commit:

| Hook | Purpose |
| --- | --- |
| `ruff` | Lint and auto-fix Python code |
| `ruff-format` | Format Python code |
| `mypy` | Type check `src/` |

Setup:

```bash
pre-commit install
```

Run manually against all files:

```bash
pre-commit run --all-files
```

## Testing

### Running Tests

```bash
uv run pytest
```

The test suite covers 30 modules with a minimum 80% coverage threshold (enforced in `pyproject.toml`). CI runs on **Ubuntu + Windows** with **Python 3.12**.

### Writing Tests

- Use `pytest` fixtures (defined in `tests/conftest.py`) for shared setup.
- Mock LLM calls via `unittest.mock`, tests must not require a running `llama-server`.
- Test each agent node independently by providing crafted `AgentState` input.
- Cover both success paths and error boundary behavior.
- Use `pytest-asyncio` for async node and router tests.

## Pull Request Process

1. Create a branch from `dev-main`: `git checkout -b dev-<feature>`.
2. Make changes and ensure linting/type checks pass.
3. Write or update tests for any behavior changes.
4. Run the full test suite locally: `uv run pytest`.
5. Push and open a pull request targeting `dev-main`.
6. Provide a clear description of the changes and their motivation.
7. Address review feedback.
8. Merged after approval and passing CI (lint, test, build, architecture check).

## Architecture Constraints

The codebase enforces strict import layering, verified by CI on every push:

```text
tools/  - Must NOT import from agents/ or rag/
rag/    - Must NOT import from agents/
agents/ - May import from rag/ and tools/
```

Violating these boundaries will fail the `architecture-check` CI job.

## Issue Reporting

When reporting issues, include:

1. Steps to reproduce.
2. Expected vs. actual behavior.
3. System information: OS version, RAM, GPU (if applicable).
4. Relevant terminal or log output.
5. Configuration overrides in `config/institution.toml`, if any.
6. The agent mode and query that triggered the issue.
