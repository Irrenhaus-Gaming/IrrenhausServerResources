class McDeployError(Exception):
    """A condition that must stop the pipeline. The CLI prints it and exits with code 1."""


def fail_if(problems: list[str], headline: str) -> None:
    """Raise one error listing every problem, so a run reports all issues at once."""
    if problems:
        raise McDeployError(headline + "\n" + "\n".join(f"  - {p}" for p in problems))
