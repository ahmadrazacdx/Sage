# Contributing

Guidelines for contributing to the Sage project.

---

## Table of Contents

- [Development Setup](#development-setup)
- [Branching Strategy](#branching-strategy)
- [Commit Conventions](#commit-conventions)
- [Code Style](#code-style)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)
- [Issue Reporting](#issue-reporting)

---

## Development Setup

Refer to [SETUP.md](SETUP.md) for complete environment setup instructions.

Quick start:

```bash
git clone https://github.com/ahmadrazacdx/Sage.git
cd Sage
uv sync --all-extras
pre-commit install
```

## Branching Strategy

| Branch | Purpose |
|---|---|
| `main` | Stable release branch |
| `dev-main` | Active development integration branch |
| `dev-<name>` | Feature branches, branched from `dev-main` |

All feature and fix branches should be merged into `dev-main` via pull request.
Releases are created by merging `dev-main` into `main` and tagging with a version
number.

## Commit Conventions

This project follows the
[Conventional Commits](https://www.conventionalcommits.org/) specification.

### Format

```
<type>(<scope>): <description>
```

### Types

| Type | Usage |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation changes |
| `style` | Code style changes (formatting, no logic change) |
| `refactor` | Code restructuring without behavior change |
| `perf` | Performance improvement |
| `test` | Adding or modifying tests |
| `ci` | CI/CD pipeline changes |
| `chore` | Build scripts, dependency updates, maintenance |

### Examples

```text
feat(quiz): add adaptive difficulty scaling
fix(research): handle timeout in digest phase
docs(setup): add Vulkan GPU setup instructions
refactor(llm): extract port allocation to utility function
```

## Code Style

### Python

- **Linter and formatter**: Ruff (configuration in `pyproject.toml`)
- **Type checking**: mypy with strict mode
- **Line length**: 120 characters
- **Docstrings**: Required for all public functions and classes
- **Imports**: Sorted by Ruff (isort rules)
- **Type annotations**: Required for all function signatures

Run checks:

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/
```

### TypeScript (Frontend)

- Standard ESLint and Prettier configuration (defined in the frontend workspace)
- Strict TypeScript mode enabled

### Pre-commit

The following hooks run automatically on each commit:

| Hook | Purpose |
|---|---|
| `ruff` | Lint and format Python code |
| `mypy` | Type check Python code |
| `conventional-commits` | Validate commit message format |

Install hooks:

```bash
pre-commit install
```

Run manually on all files:

```bash
pre-commit run --all-files
```

## Testing

### Running Tests

```bash
python -m pytest tests/
```

### Writing Tests

- Use `pytest` fixtures for shared setup.
- Mock LLM calls using `unittest.mock` to avoid requiring a running server.
- Test each agent node independently by providing crafted `AgentState` input.
- Verify both success paths and error handling.

## Pull Request Process

1. Create a feature branch from `dev-main`.
2. Make changes, ensuring all pre-commit hooks pass.
3. Write or update tests as appropriate.
4. Run the full test suite locally.
5. Push the branch and open a pull request targeting `dev-main`.
6. Provide a clear description of the changes and their motivation.
7. Address any review feedback.
8. The pull request is merged after approval and passing CI checks.

## Issue Reporting

When reporting issues, include:

1. Steps to reproduce the problem.
2. Expected behavior.
3. Actual behavior.
4. System information (OS version, RAM, GPU if applicable).
5. Relevant log output (found in the terminal or log files).
6. Configuration overrides in `config/config.toml`, if any.
